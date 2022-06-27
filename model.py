from data_generators import CustomtfDataset
from data_prepare import get_alphas

import tensorflow as tf
import pandas as pd
import numpy as np
import random
import itertools
import os

from keras import backend as K
from keras.models import Model
from keras.layers import Dense, Dropout, LeakyReLU, Activation, Input, LSTM, CuDNNLSTM, Reshape, Conv2D, Conv3D, MaxPooling2D, concatenate, Lambda, dot, BatchNormalization, Layer
from keras.callbacks import EarlyStopping, ModelCheckpoint
from keras.metrics import CategoricalAccuracy, Precision, Recall, MeanSquaredError, MeanMetricWrapper

from sklearn.metrics import classification_report, accuracy_score, mean_absolute_error, mean_squared_error, r2_score
import matplotlib.pyplot as plt
import matplotlib as mpl
import glob
from functools import partial


class CustomReshape(Layer):
    def __init__(self, output_dim, **kwargs):
        self.output_dim = output_dim
        super(CustomReshape, self).__init__(**kwargs)
    
    def build(self, input_shape):
        super(CustomReshape, self).build(input_shape)
    
    def call(self, input_data):
        batch_size = tf.shape(input_data)[0]
        T = tf.shape(input_data)[1]
        NF = tf.shape(input_data)[2]
        channels = tf.shape(input_data)[3]
        input_BID = tf.reshape(input_data[:, :, :NF//2, :], [batch_size, T, NF//2, 1, channels])
        input_BID = tf.reverse(input_BID, axis = [2])
        input_ASK = tf.reshape(input_data[:, :, NF//2:, :], [batch_size, T, NF//2, 1, channels])
        output = concatenate([input_BID, input_ASK], axis = 3)
        # if not tf.executing_eagerly():
        #     # Set the static shape for the result since it might lost during array_ops
        #     # reshape, eg, some `None` dim in the result could be inferred.
        #     output.set_shape(self.compute_output_shape(input_data.shape))
        return output

    def compute_output_shape(self, input_shape): 
        batch_size = input_shape[0]
        T = input_shape[1]
        NF = input_shape[2]
        channels = input_shape[3]
        return (batch_size, T, NF//2, 2, channels)


def weighted_categorical_crossentropy(y_true, y_pred, weights):
    # weights is a CxC matrix (where C is the number of classes) that defines the class weights
    # i.e. weights[i, j] defines the weight for an example of class i which was classified as class j
    nb_cl = len(weights)
    final_mask = K.zeros_like(y_pred[:, 0])
    y_pred_max = K.max(y_pred, axis=1)
    y_pred_max = K.reshape(y_pred_max, (K.shape(y_pred)[0], 1))
    y_pred_max_mat = K.cast(K.equal(y_pred, y_pred_max), K.floatx())
    for c_p, c_t in itertools.product(range(nb_cl), range(nb_cl)):
        final_mask += (weights[c_t, c_p] * y_pred_max_mat[:, c_p] * y_true[:, c_t])
    return K.categorical_crossentropy(y_true, y_pred) * final_mask


def multihorizon_weighted_categorical_crossentropy(y_true, y_pred, imbalances):
    # imbalances is a CxH matrix (where C is the number of classes) that defines the class imbalances
    # i.e. imbalances[:, h] defines the class distributions for horizon h
    C, H = imbalances.shape
    losses = []
    for h in range(H):
        weights = np.vstack([1 / imbalances[:, h]]*C).T
        losses.append(weighted_categorical_crossentropy(y_true[:, h, :], y_pred[:, h, :], weights))
    return tf.add_n(losses)
    

def sparse_categorical_matches(y_true, y_pred):
    """Creates float Tensor, 1.0 for label-prediction match, 0.0 for mismatch.
    You can provide logits of classes as `y_pred`, since argmax of
    logits and probabilities are same.
    Args:
        y_true: Integer ground truth values.
        y_pred: The prediction values.
    Returns:
        Match tensor: 1.0 for label-prediction match, 0.0 for mismatch.
    """
    reshape_matches = False
    y_pred = tf.convert_to_tensor(y_pred)
    y_true = tf.convert_to_tensor(y_true)
    y_true_org_shape = tf.shape(y_true)
    y_pred_rank = y_pred.shape.ndims
    y_true_rank = y_true.shape.ndims

    # If the shape of y_true is (num_samples, 1), squeeze to (num_samples,)
    if (y_true_rank is not None) and (y_pred_rank is not None) and (len(K.int_shape(y_true)) == len(K.int_shape(y_pred))):
        y_true = tf.squeeze(y_true, [-1])
        reshape_matches = True
    y_pred = tf.math.argmax(y_pred, axis=-1)

    # If the predicted output and actual output types don't match, force cast them
    # to match.
    if K.dtype(y_pred) != K.dtype(y_true):
        y_pred = tf.cast(y_pred, K.dtype(y_true))
    matches = tf.cast(tf.equal(y_true, y_pred), K.floatx())
    if reshape_matches:
        matches = tf.reshape(matches, shape=y_true_org_shape)
    return matches


class MultihorizonCategoricalAccuracy(MeanMetricWrapper):
  def __init__(self, h, name='multihorizon_categorical_accuracy', dtype=None):
    super(MultihorizonCategoricalAccuracy, self).__init__(
        lambda y_true, y_pred: sparse_categorical_matches(tf.math.argmax(y_true[:, h, :], axis=-1), y_pred[:, h, :]),
        name,
        dtype=dtype)


class MultihorizonMeanSquaredError(MeanMetricWrapper):
  def __init__(self, h, name='multihorizon_mse', dtype=None):
    super(MultihorizonMeanSquaredError, self).__init__(
        lambda y_true, y_pred: mean_squared_error(y_true[:, h], y_pred[:, h]),
        name,
        dtype=dtype)


class deepLOB:
    def __init__(self, 
                 horizon,
                 number_of_lstm,
                 data, 
                 data_dir, 
                 files = None, 
                 model_inputs = "orderbooks", 
                 T = 100,
                 levels = 10, 
                 queue_depth = None,
                 task = "classification", 
                 alphas = None, 
                 multihorizon = False, 
                 decoder = "seq2seq", 
                 n_horizons = 5,
                 batch_size = 256,
                 train_roll_window = 1,
                 imbalances = None):
        """Initialization.
        :param T: time window 
        :param levels: number of levels (note these have different meaning for orderbooks/orderflows and volumes)
        :param horizon: when not multihorizon, the horizon to consider
        :param number_of_lstm: number of nodes in lstm
        :param data: whether the data fits in the RAM and is thus divided in train, val, test datasets (and, if multihorizon, corresponding decoder_input) - "FI2010", "simulated"
                     or if custom data generator is required - "LOBSTER".
        :param data_dir: parent directory for data
        :param model_inputs: what type of inputs
        :param task: regression or classification
        :param multihorizon: whether the forecasts need to be multihorizon, if True this overrides horizon
        :param decoder: the decoder to use for multihorizon forecasts, seq2seq or attention
        :param n_horizons: the number of forecast horizons in multihorizon
        """
        self.T = T
        self.levels = levels
        if model_inputs == "orderbooks":
            self.NF = 4*levels
        elif model_inputs in ["orderflows", "volumes", "volumes_L3"]:
            self.NF = 2*levels
        else:
            raise ValueError("model_inputs must be orderbook, orderflow, volumes or volumes_L3")
        self.horizon = horizon
        if multihorizon:
            self.horizon = slice(0, n_horizons)
        self.number_of_lstm = number_of_lstm
        self.model_inputs = model_inputs
        self.queue_depth = queue_depth
        if model_inputs == "volumes_L3" and queue_depth is None:
            raise ValueError("if model_inputs is volumes_L3, queue_depth must be specified.")
        self.task = task
        self.alphas = alphas
        self.multihorizon = multihorizon
        self.decoder = decoder
        self.n_horizons = n_horizons
        self.orderbook_updates = [10, 20, 30, 50, 100]
        self.data_dir = data_dir
        self.files = files
        self.data = data
        self.batch_size = batch_size
        self.train_roll_window = train_roll_window
        self.imbalances = imbalances

        if data in ["FI2010", "simulated"]:
            train_data = np.load(os.path.join(data_dir, "train.npz"))
            trainX, trainY = train_data["X"], train_data["Y"]
            val_data = np.load(os.path.join(data_dir, "val.npz"))
            valX, valY = val_data["X"], val_data["Y"]
            test_data = np.load(os.path.join(data_dir, "test.npz"))
            testX, testY = test_data["X"], test_data["Y"]

            if not(multihorizon):
                trainY = trainY[:, self.horizon , :]
                valY = valY[:, self.horizon , :]
                testY = testY[:, self.horizon ,:]

            if multihorizon:
                train_decoder_input = np.load(os.path.join(data_dir, "train_decoder_input.npy"))
                val_decoder_input = np.load(os.path.join(data_dir, "val_decoder_input.npy"))
                test_decoder_input = np.load(os.path.join(data_dir, "test_decoder_input.npy"))

                trainX = [trainX, train_decoder_input]
                valX = [valX, val_decoder_input]
                testX = [testX, test_decoder_input]

            generator = tf.keras.preprocessing.image.ImageDataGenerator()
            self.train_generator = generator.flow(trainX, trainY, batch_size=batch_size, shuffle=True)
            self.val_generator = generator.flow(valX, valY, batch_size=batch_size, shuffle=True)
            self.test_generator = generator.flow(testX, testY, batch_size=batch_size, shuffle=False)
    
        elif data == "LOBSTER":
            if model_inputs in ["orderbooks", "orderflows"]:
                normalise = False
            elif model_inputs in ["volumes", "volumes_L3"]:
                normalise = True
            self.train_generator = CustomtfDataset(files = self.files["train"], NF = self.NF, n_horizons = self.n_horizons, model_inputs = self.model_inputs, horizon = self.horizon, alphas = self.alphas, multihorizon = self.multihorizon,window = self.T, normalise=normalise, batch_size=batch_size,  roll_window=train_roll_window)
            self.val_generator = CustomtfDataset(files = self.files["val"], NF = self.NF, n_horizons = self.n_horizons, model_inputs = self.model_inputs, horizon = self.horizon, alphas = self.alphas, multihorizon = self.multihorizon, window = self.T, normalise=normalise,  batch_size=batch_size, roll_window=train_roll_window)
            self.test_generator = CustomtfDataset(files = self.files["test"], NF = self.NF, n_horizons = self.n_horizons, model_inputs = self.model_inputs, horizon = self.horizon, alphas = self.alphas, multihorizon = self.multihorizon, window = self.T, normalise=normalise,  batch_size=batch_size, roll_window=1)

        else:
            raise ValueError('data must be either FI2010, simulated or LOBSTER.')


    def create_model(self):
        # network parameters
        if self.task == "classification":
            output_activation = "softmax"
            output_dim = 3
            if self.multihorizon:
                if self.imbalances is None:
                    loss = "categorical_crossentropy"
                else:
                    loss = partial(multihorizon_weighted_categorical_crossentropy, imbalances=self.imbalances)
                metrics = []
                for i in range(self.n_horizons):
                    h = str(self.orderbook_updates[i])
                    metrics.append([MultihorizonCategoricalAccuracy(i, name = "accuracy" + h)])
            else:
                if self.imbalances is None:
                    loss = "categorical_crossentropy"
                else:
                    weights = np.vstack([1 / imbalances[:, self.horizon]]*3).T
                    loss = partial(weighted_categorical_crossentropy, weights=weights)
                h = str(self.orderbook_updates[self.horizon])
                metrics = [CategoricalAccuracy(name = "accuracy" + h)]
        elif self.task == "regression":
            output_activation = "linear"
            output_dim = 1
            loss = "mean_squared_error"
            metrics = ["mean_squared_error"]
            if self.multihorizon:
                metrics = []
                for i in range(self.n_horizons):
                    h = str(self.orderbook_updates[i])
                    metrics.append([MultihorizonMeanSquaredError(i, name = "mse" + h)])
            else:
                h = str(self.orderbook_updates[self.horizon])
                metrics = [MeanSquaredError(name = "mse"+ h)]
        else:
            raise ValueError('task must be either classification or regression.')
        self.metrics = metrics

        adam = tf.keras.optimizers.Adam(learning_rate=0.01, epsilon=1)

        if self.model_inputs in ["orderbooks", "orderflows", "volumes"]:
            input_lmd = Input(shape=(self.T, self.NF, 1), name='input')
        elif self.model_inputs == "volumes_L3":
            input_lmd = Input(shape=(self.T, self.NF, self.queue_depth, 1), name='input')

        # build the convolutional block
        if self.model_inputs == "orderbooks":
            # [batch_size, T, NF, 1] -> [batch_size, T, NF, 32]
            conv_first1 = Conv2D(32, (1, 2), strides=(1, 2))(input_lmd)
            conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)
            # [batch_size, T, NF, 32] -> [batch_size, T, NF, 32]
            conv_first1 = Conv2D(32, (4, 1), padding='same')(conv_first1)
            conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)
            # [batch_size, T, NF, 32] -> [batch_size, T, NF, 32]
            conv_first1 = Conv2D(32, (4, 1), padding='same')(conv_first1)
            conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)

            conv_first1 = BatchNormalization(momentum=0.6)(conv_first1)

        elif self.model_inputs == "volumes":
            # [batch_size, T, NF, 1] -> [batch_size, T, NF/2, 2, 1]
            input_reshaped = CustomReshape(0)(input_lmd)
            # [batch_size, T, NF/2, 2, 1] -> [batch_size, T, NF/2-1, 1, 32]
            conv_first1 = Conv3D(32, (1, 2, 2), strides=(1, 1, 1))(input_reshaped)
            # [batch_size, T, NF/2-1, 1, 32] -> [batch_size, T, NF/2-1, 32]
            conv_first1 = Reshape((int(conv_first1.shape[1]), int(conv_first1.shape[2]), int(conv_first1.shape[4])))(conv_first1)
            conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)
            # [batch_size, T, NF/2-1, 32] -> [batch_size, T, NF/2-1, 32]
            conv_first1 = Conv2D(32, (4, 1), padding='same')(conv_first1)
            conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)
            # [batch_size, T, NF/2-1, 32] -> [batch_size, T, NF/2-1, 32]
            conv_first1 = Conv2D(32, (4, 1), padding='same')(conv_first1)
            conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)

            conv_first1 = BatchNormalization(momentum=0.6)(conv_first1)

        elif self.model_inputs == "volumes_L3":
            # [batch_size, T, NF, Q, 1] -> [batch_size, T, NF, 1, 1]
            conv_queue = Conv3D(32, (1, 1, self.queue_depth), strides = (1, 1, 1))(input_lmd)
            # [batch_size, T, NF, 1, 1] -> [batch_size, T, NF, 1]
            conv_queue = Reshape((int(conv_queue.shape[1]), int(conv_queue.shape[2]), int(conv_queue.shape[4])))(conv_queue)
            # [batch_size, T, NF, 1] -> [batch_size, T, NF/2, 2, 1]
            input_reshaped = CustomReshape(0)(conv_queue)
            # [batch_size, T, NF/2, 2, 1] -> [batch_size, T, NF/2-1, 1, 32]
            conv_first1 = Conv3D(32, (1, 2, 2), strides=(1, 1, 1))(input_reshaped)
            # [batch_size, T, NF/2-1, 1, 32] -> [batch_size, T, NF/2-1, 32]
            conv_first1 = Reshape((int(conv_first1.shape[1]), int(conv_first1.shape[2]), int(conv_first1.shape[4])))(conv_first1)
            conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)
            # [batch_size, T, NF/2-1, 32] -> [batch_size, T, NF/2-1, 32]
            conv_first1 = Conv2D(32, (4, 1), padding='same')(conv_first1)
            conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)
            # [batch_size, T, NF/2-1, 32] -> [batch_size, T, NF/2-1, 32]
            conv_first1 = Conv2D(32, (4, 1), padding='same')(conv_first1)
            conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)

            conv_first1 = BatchNormalization(momentum=0.6)(conv_first1)

        elif self.model_inputs == "orderflows":
            # [batch_size, T, NF, 1] -> [batch_size, T, NF, 1]
            conv_first1 = input_lmd

        else:
            raise ValueError('task must be either orderbooks, orderflows or volumes.')

        conv_first1 = Conv2D(32, (1, 2), strides=(1, 2))(conv_first1)
        conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)
        conv_first1 = Conv2D(32, (4, 1), padding='same')(conv_first1)
        conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)
        conv_first1 = Conv2D(32, (4, 1), padding='same')(conv_first1)
        conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)

        conv_first1 = BatchNormalization(momentum=0.6)(conv_first1)

        conv_first1 = Conv2D(32, (1, conv_first1.shape[2]))(conv_first1)
        conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)
        conv_first1 = Conv2D(32, (4, 1), padding='same')(conv_first1)
        conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)
        conv_first1 = Conv2D(32, (4, 1), padding='same')(conv_first1)
        conv_first1 = LeakyReLU(alpha=0.01)(conv_first1)

        conv_first1 = BatchNormalization(momentum=0.6)(conv_first1)

        # build the inception module
        convsecond_1 = Conv2D(64, (1, 1), padding='same')(conv_first1)
        convsecond_1 = LeakyReLU(alpha=0.01)(convsecond_1)
        convsecond_1 = Conv2D(64, (3, 1), padding='same')(convsecond_1)
        convsecond_1 = LeakyReLU(alpha=0.01)(convsecond_1)

        convsecond_1 = BatchNormalization(momentum=0.6)(convsecond_1)

        convsecond_2 = Conv2D(64, (1, 1), padding='same')(conv_first1)
        convsecond_2 = LeakyReLU(alpha=0.01)(convsecond_2)
        convsecond_2 = Conv2D(64, (5, 1), padding='same')(convsecond_2)
        convsecond_2 = LeakyReLU(alpha=0.01)(convsecond_2)

        convsecond_2 = BatchNormalization(momentum=0.6)(convsecond_2)

        convsecond_3 = MaxPooling2D((3, 1), strides=(1, 1), padding='same')(conv_first1)
        convsecond_3 = Conv2D(64, (1, 1), padding='same')(convsecond_3)
        convsecond_3 = LeakyReLU(alpha=0.01)(convsecond_3)

        convsecond_3 = BatchNormalization(momentum=0.6)(convsecond_3)

        convsecond_output = concatenate([convsecond_1, convsecond_2, convsecond_3], axis=3)
        conv_reshape = Reshape((int(convsecond_output.shape[1]), int(convsecond_output.shape[3])))(convsecond_output)
        conv_reshape = Dropout(0.2, noise_shape=(None, 1, int(conv_reshape.shape[2])))(conv_reshape, training=True)

        if not(self.multihorizon):
            # build the last LSTM layer
            conv_lstm = CuDNNLSTM(self.number_of_lstm)(conv_reshape)
            out = Dense(output_dim, activation=output_activation)(conv_lstm)
            # send to float32 for stability
            out = Activation('linear', dtype='float32')(out)
            self.model = Model(inputs=input_lmd, outputs=out)
        
        else:
            if self.decoder == "seq2seq":
                # seq2seq
                encoder_inputs = conv_reshape
                encoder = CuDNNLSTM(self.number_of_lstm, return_state=True)
                encoder_outputs, state_h, state_c = encoder(encoder_inputs)
                states = [state_h, state_c]

                # Set up the decoder, which will only process one time step at a time.
                decoder_inputs = Input(shape=(1, output_dim), name = 'decoder_input')
                decoder_lstm = CuDNNLSTM(self.number_of_lstm, return_sequences=True, return_state=True)
                decoder_dense = Dense(output_dim, activation=output_activation)

                all_outputs = []
                encoder_outputs = Reshape((1, int(encoder_outputs.shape[1])))(encoder_outputs)
                inputs = concatenate([decoder_inputs, encoder_outputs], axis=2)

                # start off decoder with
                # inputs: y_0 = decoder_inputs (exogenous), c = encoder_outputs (hidden state only)
                # hidden state: h'_0 = h_T (encoder output states: hidden state, state_h, and cell state, state_c)

                for _ in range(self.n_horizons):
                    # h'_t = f(h'_{t-1}, y_{t-1}, c)
                    outputs, state_h, state_c = decoder_lstm(inputs, initial_state=states)

                    # y_t = g(h'_t[0], c)
                    outputs = decoder_dense(concatenate([outputs, encoder_outputs], axis=2))
                    all_outputs.append(outputs)

                    # [y_t, c]
                    inputs = concatenate([outputs, encoder_outputs], axis=2)

                    # h'_t
                    states = [state_h, state_c]

                # Concatenate all predictions
                decoder_outputs = Lambda(lambda x: K.concatenate(x, axis=1))(all_outputs)
            
            elif self.decoder == "attention":
                # attention
                encoder_inputs = conv_reshape
                encoder = CuDNNLSTM(self.number_of_lstm, return_state=True, return_sequences=True)
                encoder_outputs, state_h, state_c = encoder(encoder_inputs)
                states = [state_h, state_c]

                # Set up the decoder, which will only process one time step at a time.
                # The attention decoder will have a different context vector at each time step, depending on attention weights.
                decoder_inputs = Input(shape=(1, output_dim))
                decoder_lstm = CuDNNLSTM(self.number_of_lstm, return_sequences=True, return_state=True)
                decoder_dense = Dense(output_dim, activation=output_activation, name='output_layer')

                # start off decoder with
                # inputs: y_0 = decoder_inputs (exogenous), c = encoder_state_h (h_T[0], final hidden state only)
                # hidden state: h'_0 = h_T (encoder output states: hidden state, state_h, and cell state, state_c)

                encoder_state_h = Reshape((1, int(state_h.shape[1])))(state_h)
                inputs = concatenate([decoder_inputs, encoder_state_h], axis=2)

                all_outputs = []
                all_attention = []

                for _ in range(self.n_horizons):
                    # h'_t = f(h'_{t-1}, y_{t-1}, c_{t-1})
                    outputs, state_h, state_c = decoder_lstm(inputs, initial_state=states)

                    # dot attention weights, alpha_{i,t} = exp(h_i h'_{t}) / sum_{i=1}^T exp(h_i h'_{t})
                    attention = dot([outputs, encoder_outputs], axes=2)
                    attention = Activation('softmax')(attention)

                    # context vector, weighted average of all hidden states of encoder, weights determined by attention
                    # c_{t} = sum_{i=1}^T alpha_{i, t} h_i
                    context = dot([attention, encoder_outputs], axes=[2, 1])

                    # y_t = g(h'_t, c_t)
                    decoder_combined_context = concatenate([context, outputs])
                    outputs = decoder_dense(decoder_combined_context)
                    all_outputs.append(outputs)
                    all_attention.append(attention)
                    
                    # [y_t, c_t]
                    inputs = concatenate([outputs, context], axis=2)

                    # h'_t
                    states = [state_h, state_c]

                # Concatenate all predictions
                decoder_outputs = Lambda(lambda x: K.concatenate(x, axis=1), name='outputs')(all_outputs)
                # decoder_attention = Lambda(lambda x: K.concatenate(x, axis=1), name='attentions')(all_attention)
            
            elif self.decoder == None:
                pass

            else:
                raise ValueError('decoder must be either seq2seq or attention.')

            # send to float32 for stability
            decoder_outputs = Activation('linear', dtype='float32')(decoder_outputs)
            self.model = Model(inputs=[input_lmd, decoder_inputs], outputs=decoder_outputs)
        
        self.model.compile(loss=loss, metrics=metrics, optimizer=adam)

    def fit_model(self, epochs, checkpoint_filepath, load_weights, load_weights_filepath, verbose=1, batch_size = 256, patience=5):
        model_checkpoint_callback = ModelCheckpoint(filepath=checkpoint_filepath,
        					                        save_weights_only=True,
						                            monitor='val_loss',
                                                    mode='auto',
                                                    save_best_only=True)
        
        early_stopping = EarlyStopping(monitor='val_loss', patience=patience, mode='auto')

        if load_weights == True:
            self.model.load_weights(load_weights_filepath)

        self.model.fit(self.train_generator, validation_data=self.val_generator,
                       epochs=epochs, verbose=verbose, workers=8,
                       max_queue_size=10, use_multiprocessing=True,
                       callbacks=[model_checkpoint_callback, early_stopping])

    def evaluate_model(self, load_weights_filepath, eval_set = "test"):
        self.model.load_weights(load_weights_filepath)

        print("Evaluating performance on ", eval_set, "set...")

        if eval_set == "test":
            generator = self.test_generator
            roll_window = 1
        elif eval_set == "val":
            generator = self.val_generator
            roll_window = self.train_roll_window
        elif eval_set == "train":
            generator = self.train_generator
            roll_window = self.train_roll_window
        else:
            raise ValueError("eval_set must be test, val or train.")
        
        predY = np.squeeze(self.model.predict(generator, verbose=2))

        if self.data in ["FI2010", "simulated"]:
            eval_data = np.load(os.path.join(self.data_dir, eval_set + ".npz"))
            evalY = eval_data["Y"][:, self.horizon, ...]
        if self.data == "LOBSTER":
            eval_files = self.files[eval_set]
            evalY = np.array([])
            if self.multihorizon:
                evalY = evalY.reshape(0, self.n_horizons)
            for file in eval_files:
                if self.model_inputs in ["orderbooks", "orderflows"]:
                    data = pd.read_csv(file).to_numpy()
                    responses = data[:, -self.n_horizons:]
                elif self.model_inputs[:7] == "volumes":
                    data = np.load(file)
                    responses = data['responses']
                evalY = np.concatenate([evalY, responses[(self.T-1)::roll_window, self.horizon]])
                # evalY = np.concatenate([evalY, responses[:, self.horizon]])
            # evalY = evalY[(self.T-1)::roll_window]

            if self.task == "classification":
                if self.multihorizon:
                    all_label = []
                    for h in range(evalY.shape[1]):
                        one_label = (+1)*(evalY[:, h]>=-self.alphas[h]) + (+1)*(evalY[:, h]>self.alphas[h])
                        one_label = tf.keras.utils.to_categorical(one_label, 3)
                        one_label = one_label.reshape(len(one_label), 1, 3)
                        all_label.append(one_label)
                    evalY = np.hstack(all_label)
                else:
                    evalY = (+1)*(evalY>=-self.alphas[self.horizon]) + (+1)*(evalY>self.alphas[self.horizon])
                    evalY = tf.keras.utils.to_categorical(evalY, 3)
            
        if self.task == "classification":
            if not self.multihorizon:
                print("Prediction horizon:", self.orderbook_updates[self.horizon], " orderbook updates")
                print('accuracy_score:', accuracy_score(np.argmax(evalY, axis=1), np.argmax(predY, axis=1)))
                print(classification_report(np.argmax(evalY, axis=1), np.argmax(predY, axis=1), digits=4))
            else:
                for h in range(self.n_horizons):
                    print("Prediction horizon:", self.orderbook_updates[h], " orderbook updates")
                    print('accuracy_score:', accuracy_score(np.argmax(evalY[:, h, :], axis=1), np.argmax(predY[:, h, :], axis=1)))
                    print(classification_report(np.argmax(evalY[:, h, :], axis=1), np.argmax(predY[:, h, :], axis=1), digits=4))
        elif self.task == "regression":
            if not self.multihorizon:
                print("Prediction horizon:", self.orderbook_updates[self.horizon], " orderbook updates")
                print('MSE:', mean_squared_error(evalY, predY))
                print('MAE:', mean_absolute_error(evalY, predY))
                print('r2:', r2_score(evalY, predY))
                regression_fit_plot(evalY, predY, title = eval_set + str(self.orderbook_updates[self.horizon]), 
                                    path = os.path.join("plots", self.data, "single-horizon", eval_set + str(self.orderbook_updates[self.horizon]) + '.png'))
                
            else:
                for h in range(self.n_horizons):
                    print("Prediction horizon:", self.orderbook_updates[h], " orderbook updates")
                    print('MSE:', mean_squared_error(evalY[:, h], predY[:, h]))
                    print('MAE:', mean_absolute_error(evalY[:, h], predY[:, h]))
                    print('r2:', r2_score(evalY[:, h], predY[:, h]))
                    regression_fit_plot(evalY, predY, title = eval_set + str(self.orderbook_updates[h]), 
                                        path=os.path.join("plots", self.data, "multi-horizon", self.decoder, eval_set + str(self.orderbook_updates[h]) + '.png'))


def regression_fit_plot(evalY, predY, title, path):
    fig, ax = plt.subplots()
    mpl.rcParams['agg.path.chunksize'] = len(evalY)
    ax.scatter(evalY, predY, s=10, c='k', alpha=0.5)
    lims = [np.min([evalY, predY]), np.max([evalY, predY])]
    ax.plot(lims, lims, linestyle='--', color='k', alpha=0.75, zorder=0)
    ax.set_aspect('equal')
    ax.set_xlim(lims)
    ax.set_ylim(lims)
    ax.set_title(title)
    ax.set_xlabel("True y")
    ax.set_ylabel("Pred y")
    fig.savefig(path)

if __name__ == '__main__':
    # limit gpu memory
    gpus = tf.config.experimental.list_physical_devices('GPU')
    if gpus:
        try:
            # Use only one GPUs
            tf.config.set_visible_devices(gpus[0], 'GPU')
            logical_gpus = tf.config.list_logical_devices('GPU')

            # Or use all GPUs, memory growth needs to be the same across GPUs
            # for gpu in gpus:
            #     tf.config.experimental.set_memory_growth(gpu, True)
            # logical_gpus = tf.config.experimental.list_logical_devices('GPU')
            print(len(gpus), "Physical GPUs,", len(logical_gpus), "Logical GPUs")
        except RuntimeError as e:
            # Memory growth must be set before GPUs have been initialized
            print(e)

    # set random seeds
    random.seed(0)
    np.random.seed(1)
    tf.random.set_seed(2)

    tf.keras.mixed_precision.set_global_policy('mixed_float16')
    
    orderbook_updates = [10, 20, 30, 50, 100]
    
    #################################### SETTINGS ########################################
    model_inputs = "orderbooks"                    # options: "orderbooks", "orderflows", "volumes", "volumes_L3"
    data = "LOBSTER"                              # options: "FI2010", "LOBSTER", "simulated"
    data_dir = "data/AAL_orderbooks"
    csv_file_list = glob.glob(os.path.join(data_dir, "*.{}").format("csv"))
    csv_file_list.sort()
    val_train_list = csv_file_list[:25]
    random.shuffle(val_train_list)
    test_list = csv_file_list[25:30]
    files = {
        "val": val_train_list[:5],
        "train": val_train_list[5:25],
        "test": test_list
    }
    # alphas, distributions = get_alphas(files["train"])
    alphas = np.array([0.0000000e+00, 0.0000000e+00, 0.0000000e+00, 0.0000000e+00, 3.2942814e-05])
    distributions = pd.DataFrame(np.vstack([np.array([0.121552, 0.194825, 0.245483, 0.314996, 0.334330]), 
                                            np.array([0.752556, 0.604704, 0.504695, 0.368647, 0.330456]),
                                            np.array([0.125893, 0.200471, 0.249821, 0.316357, 0.335214])]), 
                                 index=["down", "stationary", "up"], 
                                 columns=["10", "20", "30", "50", "100"])
    imbalances = distributions.to_numpy()
    # imbalances = None
    task = "classification"
    multihorizon = True                         # options: True, False
    decoder = "seq2seq"                         # options: "seq2seq", "attention"

    T = 100
    levels = 1                                  # remember to change this when changing features
    queue_depth = 10                            # for L3 data only
    n_horizons = 5
    horizon = 0                                 # prediction horizon (0, 1, 2, 3, 4) -> (10, 20, 30, 50, 100) orderbook events
    epochs = 50
    patience = 10
    training_verbose = 2
    train_roll_window = 100
    batch_size = 256                            # note we use 256 for LOBSTER, 32 for FI2010 or simulated
    number_of_lstm = 64

    checkpoint_filepath = './model_weights/test_model'
    load_weights = False
    load_weights_filepath = './model_weights/test_model'

    #######################################################################################

    model = deepLOB(T = T, 
                    levels = levels, 
                    horizon = horizon, 
                    number_of_lstm = number_of_lstm, 
                    data = data, 
                    data_dir = data_dir, 
                    files = files, 
                    model_inputs = model_inputs, 
                    queue_depth = queue_depth,
                    task = task, 
                    alphas = alphas, 
                    multihorizon = multihorizon, 
                    decoder = decoder, 
                    n_horizons = n_horizons,
                    train_roll_window = train_roll_window,
                    imbalances = imbalances)

    model.create_model()

    # model.model.summary()

    model.fit_model(epochs = epochs,
                    checkpoint_filepath = checkpoint_filepath,
                    load_weights = load_weights,
                    load_weights_filepath = load_weights_filepath,
                    verbose = training_verbose,
                    batch_size = batch_size,
                    patience = patience)

    model.evaluate_model(load_weights_filepath = load_weights_filepath, eval_set = "test")
    model.evaluate_model(load_weights_filepath = load_weights_filepath, eval_set = "train")
    model.evaluate_model(load_weights_filepath = load_weights_filepath, eval_set = "val")     
