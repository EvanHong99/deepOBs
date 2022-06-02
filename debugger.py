from importlib_metadata import distributions
import numpy as np
import pandas as pd
import glob
import os
import tensorflow as tf
from keras.layers import Reshape, concatenate
from data_generators import CustomtfDataset
from data_prepare import get_alphas

if __name__ == '__main__':
    # limit gpu memory
    gpus = tf.config.experimental.list_physical_devices('GPU')
    if gpus:
        try:
            # Use only one GPUs
            tf.config.set_visible_devices(gpus[1], 'GPU')
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
    np.random.seed(1)
    tf.random.set_seed(2)

    data_dir = "data/AAL_orderbooks"
    csv_file_list = glob.glob(os.path.join(data_dir, "*.{}").format("csv"))
    csv_file_list.sort()
    files = {
        "val": csv_file_list[:5],
        "train": csv_file_list[5:25],
        "test": csv_file_list[25:30]
    }
    # alphas = get_alphas(files["train"])
    alphas = np.array([0.0000000e+00, 0.0000000e+00, 0.0000000e+00, 0.0000000e+00, 3.2942814e-05])
    NF = 40
    horizon = 0
    multihorizon=True
    # data_gen = CustomDataGenerator2(data_dir, files["train"], NF, horizon, task = "classification", alphas = alphas, multihorizon = True, normalise = False, batch_size=256, shuffle=False, teacher_forcing=False, window=100)
    # n_batches = data_gen.__len__()
    # print(n_batches)
    # for i in range(n_batches):
    #     x_batch, y_batch = data_gen.__getitem__(i)
    #     if i==0:
    #         print(x_batch)
    #         print(y_batch)
    #     if not(x_batch[0].shape == (256, 100, 40, 1)):
    #         print(x_batch[0].shape)
    #     if not(y_batch.shape == (256, 5, 3)):
    #         print(y_batch.shape)

    tf_dataset = CustomtfDataset(files["val"], NF, horizon, task = "classification", alphas = alphas, multihorizon = True, normalise = False, batch_size=256, shuffle=False, teacher_forcing=False, window=100)
    
    # if multihorizon:
    #     counter = [{}]*5
    # else:
    #     counter = {}
    # for element in tf_dataset:
    #     labels = element[1]
    #     for i in range(labels.shape[0]):
    #         if multihorizon:
    #             for j in range(labels.shape[1]):
    #                 label = int(tf.where(labels[i, j, :] == 1).numpy())
    #                 counter[j][label] = counter[j].get(label, 0) + 1
    #         else:
    #             label = int(tf.where(labels[i, :] == 1).numpy())
    #             counter[label] = counter.get(label, 0) + 1

    # alphas, distributions_ = get_alphas(files["train"], distribution=True)
    distributions_ = pd.DataFrame(np.vstack([np.array([0.121552, 0.194825, 0.245483, 0.314996, 0.334330]), 
                                            np.array([0.752556, 0.604704, 0.504695, 0.368647, 0.330456]),
                                            np.array([0.125893, 0.200471, 0.249821, 0.316357, 0.335214])]), 
                                index=["down", "stationary", "up"], 
                                columns=["10", "20", "30", "50", "100"])
    print(distributions_)