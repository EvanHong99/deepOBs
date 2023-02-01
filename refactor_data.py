import os
import glob
import pandas as pd
import re
import datetime
import numpy as np
import re
from multiprocessing import Pool

def multiprocess_orderbooks(TICKER, input_path, output_path, log_path, horizons=np.array([10, 20, 30, 50, 100]), NF_volume=40, queue_depth=10, smoothing="uniform", k=10):
    """
    Pre-process LOBSTER data into feature-response pairs parallely. The data must be stored in the input_path
    directory as daily message book and order book files. For details of processing steps see description of process_orderbook function.
    A log file is produced tracking:
    - order book files with problems
    - message book files with problems
    - trading days with unusual open - close times
    - trading days with crossed quotes
    :param TICKER: the TICKER to be considered, str
    :param input_path: the path where the order book and message book files are stored, order book files have shape (:, 4*levels):
                       "ASKp1", "ASKs1", "BIDp1",  "BIDs1", ..., "ASKplevels", "ASKslevels", "BIDplevels",  "BIDslevels", str
    :param output_path: the path where we wish to save the processed datasets (as .npz files), str
    :param log_path: the path where processing logs are saved, str
    :param NF_volume: number of features for volume representation, only used if volume in features
               for volumes NF = 2W where W is number of ticks on each side of mid, int
               for orderbooks NF = 4*levels
               for ordeflows NF = 2*levels
    :param horizons: forecasting horizons for labels, (h,) array
    :param queue_depth: the depth beyond which to aggregate the queue for volume features, int
    :param smoothing: whether to use "uniform" or "horizon" smoothing, bool
    :param k: smoothing window for returns when smoothing = "uniform", int
    :return: saves the processed features in output_path, as .npz files with numpy attributes 
             "orderbook_features" (:, 4*levels) 
             "orderflow_features" (:, 2*levels) 
             "volume_features" (:, NF_volume, queue_depth)
             "responses" (:, h).
    """
    csv_file_list = glob.glob(os.path.join(input_path, "*.{}".format("csv")))

    csv_orderbook = [name for name in csv_file_list if "orderbook" in name]
    csv_message = [name for name in csv_file_list if "message" in name]

    csv_orderbook.sort()
    csv_message.sort()

    # check if exactly half of the files are order book and exactly half are messages
    assert (len(csv_message) == len(csv_orderbook))
    assert (len(csv_file_list) == len(csv_message) + len(csv_orderbook))

    print("started multiprocessing")
    
    try:
        pool = Pool(os.cpu_count() - 2)  # leave 2 cpus free
        engine = ProcessOrderbook(TICKER, output_path, NF_volume, queue_depth, horizons, smoothing, k)
        logs = pool.map(engine, csv_orderbook)
    finally: # To make sure processes are closed in the end, even if errors happen
        pool.close()
        pool.join()

    print("finished multiprocessing")

    with open(log_path + "/processing_logs.txt", "w") as f:
        for log in logs:
            f.write(log + "\n")

    print("please check processing logs.")


class ProcessOrderbook(object):
    """
    Multiprocessing engine for pre-processing LOBSTER data into feature-response npz files.
    """
    def __init__(self, TICKER, output_path, NF_volume, queue_depth, horizons, smoothing, k):
        self.TICKER = TICKER
        self.output_path = output_path
        self.NF_volume = NF_volume
        self.queue_depth = queue_depth
        self.horizons = horizons
        self.smoothing = smoothing
        self.k = k

    def __call__(self, orderbook_name):
        output = process_orderbook(orderbook_name, self.TICKER, self.output_path, self.NF_volume, self.queue_depth, self.horizons, self.smoothing, self.k)
        return output


def process_orderbook(orderbook_name, TICKER, output_path, NF_volume, queue_depth, horizons, smoothing, k):
    """
    Function carrying out processing of single order book orderbook_name. Features are not normalised.
    The data is treated in the following way:
    - order book states with crossed quotes are removed.
    - each state in the orderbook is time-stamped, with states occurring at the same time collapsed
      onto the last state.
    - the first and last 10 minutes of market activity (inside usual opening times) are dropped.
    - the smoothed returns at the requested horizons (in order book changes) are returned
      if smoothing = "horizon": l = (m+ - m)/m, where m+ denotes the mean of the next h mid-prices, m(.) is current mid price.
      if smoothing = "uniform": l = (m+ - m)/m, where m+ denotes the mean of the k+1 mid-prices centered at m(. + h), m(.) is current mid price.
    :param orderbook_name: order book to process, of shape (T, 4*levels):
                          "ASKp1", "ASKs1", "BIDp1",  "BIDs1", ..., "ASKplevels", "ASKslevels", "BIDplevels",  "BIDslevels",
                          str
    :param TICKER: the TICKER to be considered, str
    :param output_path: the path where we wish to save the processed datasets (as .npz files), str
    :param NF_volume: number of features for volume representation, only used if volume in features
               for volumes NF = 2W where W is number of ticks on each side of mid, int
               for orderbooks NF = 4*levels
               for ordeflows NF = 2*levels
    :param queue_depth: the depth beyond which to aggregate the queue for volume features, int
    :param horizons: forecasting horizons for labels, (h,) array
    :param smoothing: whether to use "uniform" or "horizon" smoothing, bool
    :param k: smoothing window for returns when smoothing = "uniform", int
    :return: saves the processed features in output_path, as .npz files with numpy attributes 
             "orderbook_features" (:, 4*levels) 
             "orderflow_features" (:, 2*levels) 
             "volume_features" (:, NF_volume, queue_depth)
             "responses" (:, h).
    """
    print(orderbook_name)
    log = ''

    ### load LOBSTER orderbook and message files
    try:
        df_orderbook = pd.read_csv(orderbook_name, header=None)
    except:
        return orderbook_name + ' skipped. Error: failed to read orderbook.'

    levels = int(df_orderbook.shape[1] / 4)
    feature_names_raw = ["ASKp", "ASKs", "BIDp", "BIDs"]
    orderbook_feature_names = []
    for i in range(1, levels + 1):
        for j in range(4):
            orderbook_feature_names += [feature_names_raw[j] + str(i)]
    df_orderbook.columns = orderbook_feature_names

    # compute the mid prices
    df_orderbook.insert(0, "mid price", (df_orderbook["ASKp1"] + df_orderbook["BIDp1"]) / 2)

    # get date
    match = re.findall(r"\d{4}-\d{2}-\d{2}", orderbook_name)[-1]
    date = datetime.datetime.strptime(match, "%Y-%m-%d")

    # read times from message file. keep a record of problematic files
    message_name = orderbook_name.replace("orderbook", "message")
    try:
        df_message = pd.read_csv(message_name, usecols=[0, 1, 2, 3, 4, 5], header=None)
    except:
        return orderbook_name + ' skipped. Error: failed to read messagebook.'

    # check the two df have the same length
    assert (len(df_message) == len(df_orderbook))

    # add column names to message book
    df_message.columns = ["seconds", "event type", "order ID", "volume", "price", "direction"]

    ### process potential data anomalies

    # 1. remove trading halts
    trading_halts_start = df_message[(df_message["event type"] == 7)&(df_message["price"] == -1)].index
    trading_halts_end = df_message[(df_message["event type"] == 7)&(df_message["price"] == 1)].index
    trading_halts_index = np.array([])
    for halt_start, halt_end in zip(trading_halts_start, trading_halts_end):
        trading_halts_index = np.append(trading_halts_index, df_message.index[(df_message.index >= halt_start)&(df_message.index < halt_end)])
    if len(trading_halts_index) > 0:
        for halt_start, halt_end in zip(trading_halts_start, trading_halts_end):
            log = log + 'Warning: trading halt between ' + str(df_message.loc[halt_start, "seconds"]) + ' and ' + str(df_message.loc[halt_end, "seconds"]) + ' in ' + orderbook_name + '.\n'
    df_orderbook = df_orderbook.drop(trading_halts_index)
    df_message = df_message.drop(trading_halts_index)

    # 2. remove crossed quotes
    crossed_quotes_index = df_orderbook[(df_orderbook["BIDp1"] > df_orderbook["ASKp1"])].index
    if len(crossed_quotes_index) > 0:
        log = log + 'Warning: ' + str(len(crossed_quotes_index)) + ' crossed quotes removed in ' + orderbook_name + '.\n'
    df_orderbook = df_orderbook.drop(crossed_quotes_index)
    df_message = df_message.drop(crossed_quotes_index)

    # add the seconds since midnight column to the order book from the message book
    df_orderbook.insert(0, "seconds", df_message["seconds"])

    df_orderbook_full = df_orderbook
    df_message_full = df_message

    # 3. one conceptual event (e.g. limit order modification which is implemented as a cancellation followed
    # by an immediate new arrival, single market order executing against multiple resting limit orders) may
    # appear as multiple rows in the message file, all with the same timestamp.
    # We hence group the order book data by unique timestamps and take the last entry.
    df_orderbook = df_orderbook.groupby(["seconds"]).tail(1)
    df_message = df_message.groupby(["seconds"]).tail(1)

    # 4. check market opening times for strange values
    market_open = int(df_orderbook["seconds"].iloc[0] / 60) / 60  # open at minute before first transaction
    market_close = (int(df_orderbook["seconds"].iloc[-1] / 60) + 1) / 60  # close at minute after last transaction

    if not (market_open == 9.5 and market_close == 16):
        log = log + 'Warning: unusual opening times in ' + orderbook_name + ': ' + str(market_open) + ' - ' + str(market_close) + '.\n'
    
    # drop values outside of market hours
    df_orderbook = df_orderbook.loc[(df_orderbook["seconds"] >= 34200) &
                                    (df_orderbook["seconds"] <= 57600)]
    df_message = df_message.loc[(df_message["seconds"] >= 34200) &
                                (df_message["seconds"] <= 57600)]
    
    ### compute orderflow features
    ASK_prices = df_orderbook.loc[:, df_orderbook.columns.str.contains('ASKp')]
    BID_prices = df_orderbook.loc[:, df_orderbook.columns.str.contains('BIDp')]
    ASK_sizes = df_orderbook.loc[:, df_orderbook.columns.str.contains('ASKs')]
    BID_sizes = df_orderbook.loc[:, df_orderbook.columns.str.contains('BIDs')]

    ASK_price_changes = ASK_prices.diff().dropna().to_numpy()
    BID_price_changes = BID_prices.diff().dropna().to_numpy()
    ASK_size_changes = ASK_sizes.diff().dropna().to_numpy()
    BID_size_changes = BID_sizes.diff().dropna().to_numpy()

    ASK_sizes = ASK_sizes.to_numpy()
    BID_sizes = BID_sizes.to_numpy()

    ASK_OF = (ASK_price_changes > 0.0) * (-ASK_sizes[:-1, :]) + (ASK_price_changes == 0.0) * ASK_size_changes + (ASK_price_changes < 0) * ASK_sizes[1:, :]
    BID_OF = (BID_price_changes < 0.0) * (-BID_sizes[:-1, :]) + (BID_price_changes == 0.0) * BID_size_changes + (BID_price_changes > 0) * BID_sizes[1:, :]

    # remove all price-volume features and add in orderflow
    df_orderflow = pd.DataFrame([], index = df_orderbook.index[1:])
    feature_names_raw = ["ASK_OF", "BID_OF"]
    orderflow_feature_names = []
    for feature_name in feature_names_raw:
        for i in range(1, levels + 1):
            orderflow_feature_names += [feature_name + str(i)]
    df_orderflow[orderflow_feature_names] = np.concatenate([ASK_OF, BID_OF], axis=1)
    
    # re-order columns
    feature_names_reordered = [[]]*len(orderflow_feature_names)
    feature_names_reordered[::2] = orderflow_feature_names[:levels]
    feature_names_reordered[1::2] = orderflow_feature_names[levels:]
    orderflow_feature_names = feature_names_reordered

    df_orderflow = df_orderflow[orderflow_feature_names]

    # 6. drop first and last 10 minutes of trading using seconds (do this after orderflow computation)
    market_open_seconds = market_open * 60 * 60 + 10 * 60
    market_close_seconds = market_close * 60 * 60 - 10 * 60
    df_orderbook = df_orderbook.loc[(df_orderbook["seconds"] >= market_open_seconds) &
                                    (df_orderbook["seconds"] <= market_close_seconds)]

    df_message = df_message.loc[(df_message["seconds"] >= market_open_seconds) &
                                (df_message["seconds"] <= market_close_seconds)]

    df_orderflow = df_orderflow.loc[df_orderbook.index]
    
    ### compute volume features
    volumes = np.zeros((len(df_orderbook_full), NF_volume, queue_depth))
    
    # Assumes tick_size = 0.01 $, as per LOBSTER data
    ticks = np.hstack((np.outer(np.round((df_orderbook_full["mid price"] - 25) / 100) * 100, np.ones(NF_volume//2)) + 100 * np.outer(np.ones(len(df_orderbook_full)), np.arange(-NF_volume//2+1, 1)),
                        np.outer(np.round((df_orderbook_full["mid price"] + 25) / 100) * 100, np.ones(NF_volume//2)) + 100 * np.outer(np.ones(len(df_orderbook_full)), np.arange(NF_volume//2))))
    ticks = ticks.astype(int)

    orderbook_states = df_orderbook_full[orderbook_feature_names]
    orderbook_states_prices = orderbook_states.values[:, ::2]
    orderbook_states_volumes = orderbook_states.values[:, 1::2]

    # volumes = np.zeros((len(df_orderbook_full), NF_volume))

    # for i in range(NF_volume):
    #     # match tick prices with prices in levels of orderbook
    #     flags = (orderbook_states_prices == np.repeat(ticks[:, i].reshape((len(orderbook_states_prices), 1)), orderbook_states_prices.shape[1], axis=1))
    #     volumes[flags.sum(axis=1) > 0, i] = orderbook_states_volumes[flags]

    prices_dict = {}
    
    # skip first message_df row
    skip = True
    
    for i, index in enumerate(df_message_full.index):
        seconds = df_message_full.loc[index, "seconds"]
        event_type = df_message_full.loc[index, "event type"]
        order_id = df_message_full.loc[index, "order ID"]
        price = df_message_full.loc[index, "price"]
        volume = df_message_full.loc[index, "volume"]

        # if new price is entering range (re-)initialize dict
        for price_ in orderbook_states_prices[i, :]:
            if (price_ in orderbook_states_prices[i - 1, :]) and (price_ in prices_dict.keys()):
                pass
            else:
                j = np.where(orderbook_states_prices[i, :]==price_)[0][0]
                volume_ = orderbook_states_volumes[i, j]
                prices_dict[price_] = pd.DataFrame(np.array([[volume_, 0]]), index = ['id'], columns = ["volume", "seconds"])
                if price_ == price:
                    skip = True
        
        price_df = prices_dict.get(price, pd.DataFrame([], columns = ["volume", "seconds"], dtype=float))
        
        # if new price also corresponds to message_df price skip to avoid double counting
        if skip:
            skip = False
            pass
        
        # if the price from message_df is not in df_orderbook prices, i.e. a failure in LOBSTER reconstruction system, treat as hidden market order
        elif (not price in orderbook_states_prices[(i-1):(i+1), :])&(event_type in [1, 2, 3, 4, 5]):
            event_type = 5
            pass

        # new limit order
        elif event_type == 1:
            price_df.loc[order_id] = [volume, seconds]
        
        # cancellation (partial deletion)
        elif event_type == 2:
            # if order_id is not in price dataframe then this is part of initial order
            if order_id in price_df.index:
                price_df.loc[order_id, "volume"] -= volume
            else:
                price_df.loc["id", "volume"] -= volume
        
        # deletion
        elif event_type == 3:
            # if id is not present (i.e. it is at the start of order book), delete from "id" and check if "id" is empty
            if order_id in price_df.index:
                price_df = price_df.drop(order_id)
            else:
                price_df.loc["id", "volume"] -= volume
                if price_df.loc["id", "volume"] == 0:
                    price_df = price_df.drop("id")
        
        # execution of visible limit order
        elif event_type == 4:
            if order_id in price_df.index:
                price_df.loc[order_id, "volume"] -= volume
                if price_df.loc[order_id, "volume"] == 0:
                    price_df = price_df.drop(order_id)
            else:
                price_df.loc["id", "volume"] -= volume
                if price_df.loc["id", "volume"] == 0:
                    price_df = price_df.drop("id")
        
        # execution of hidden limit order
        elif event_type == 5:
            pass
        
        # auction trade
        elif event_type == 6:
            pass
        
        # trading halt
        elif event_type == 7:
            # re-initialize prices_dict
            prices_dict = {}
            for price_ in orderbook_states_prices[i, :]:
                j = np.where(orderbook_states_prices[i, :]==price_)[0][0]
                volume_ = orderbook_states_volumes[i, j]
                prices_dict[price_] = pd.DataFrame(np.array([[volume_, 0]]), index = ['id'], columns = ["volume", "seconds"])
            pass
        
        else:
            raise ValueError("LOBSTER event type must be 1, 2, 3, 4, 5, 6 or 7")
        
        price_df = price_df.sort_values(by="seconds")
        prices_dict[price] = price_df

        # update orderbooks_L3
        if event_type in [5, 6]:
            volumes[i, :, :] = volumes[i - 1, :, :] 
        elif (ticks[i, :] == ticks[i - 1, :]).all() & (not event_type == 7):
            if price in ticks[i, :]:
                j = np.where(ticks[i, :] == price)[0][0]
                volumes[i, :, :] = volumes[i - 1, :, :] 
                if len(price_df) == 0:
                    volumes[i, j, :] = np.zeros(queue_depth)
                elif len(price_df) < queue_depth:
                    volumes[i, j, :len(price_df)] = price_df["volume"].values
                    volumes[i, j, len(price_df):] = 0
                else:
                    volumes[i, j, :(queue_depth-1)] = price_df["volume"].values[:(queue_depth-1)]
                    volumes[i, j, queue_depth-1] = np.sum(price_df["volume"].values[(queue_depth-1):])
            else:
                volumes[i, :, :] = volumes[i - 1, :, :]
        else:
            for j, price_ in enumerate(ticks[i, :]):
                price_df_ = prices_dict.get(price_, [])
                if len(price_df_) == 0:
                    volumes[i, j, :] = np.zeros(queue_depth)
                elif len(price_df_) < queue_depth:
                    volumes[i, j, :len(price_df_)] = price_df_["volume"].values
                else:
                    volumes[i, j, :(queue_depth-1)] = price_df_["volume"].values[:(queue_depth-1)]
                    volumes[i, j, queue_depth-1] = np.sum(price_df_["volume"].values[(queue_depth-1):])
        
    # need then to remove all same timestamps (collapse to last) and first/last 10 minutes
    volumes = volumes[df_orderbook_full.index.isin(df_orderbook.index), :, :]

    ### compute responses
    if smoothing == "horizon":
        # create labels for returns with smoothing labelling method
        for h in horizons:
            rolling_mid = df_orderbook["mid price"].rolling(h).mean().dropna()[1:]
            rolling_mid = rolling_mid.to_numpy().flatten()
            smooth_pct_change = rolling_mid/df_orderbook["mid price"][:-h] - 1
            df_orderbook[str(h)] = np.concatenate((smooth_pct_change, np.repeat(np.NaN, int(h))))
    elif smoothing == "uniform":
        # create labels for returns with smoothing labelling method
        rolling_mid = df_orderbook["mid price"].rolling(k+1, center=True).mean()
        rolling_mid = rolling_mid.to_numpy().flatten()
        for h in horizons:
            smooth_pct_change = rolling_mid[h:]/df_orderbook["mid price"][:-h] - 1
            df_orderbook[str(h)] = smooth_pct_change

    # drop seconds and mid price columns
    df_orderbook = df_orderbook.drop(["seconds", "mid price"], axis=1)

    # drop elements with na predictions at the end which cannot be used for training
    if smoothing == "horizon":
        k = 0
    orderbook_features = df_orderbook.iloc[:-(max(horizons)+k//2), :-len(horizons)].values
    orderflow_features = df_orderflow.iloc[:-(max(horizons)+k//2), :].values
    volume_features = volumes[:-(max(horizons)+k//2), :, :]
    returns = df_orderbook.iloc[:-(max(horizons)+k//2), -len(horizons):].values

    # save
    output_name = os.path.join(output_path, TICKER + "_" + "data" + "_" + str(date.date()))
    np.savez(output_name + ".npz", orderbook_features=orderbook_features, orderflow_features=orderflow_features, volume_features=volume_features, responses=returns)

    return log + orderbook_name + ' completed.'

def refactor_data(TICKER):
    logs = []
    # paths
    orderbooks_dir = 'data/' + TICKER + '_orderbooks'
    orderflows_dir = 'data/' + TICKER + '_orderflows'
    volumes_dir = 'data/' + TICKER + '_volumes'
    output_path = 'data/' + TICKER
    # files
    orderbooks_file_list = glob.glob(os.path.join(orderbooks_dir, "*.{}".format("csv")))
    orderflows_file_list = glob.glob(os.path.join(orderflows_dir, "*.{}".format("csv")))
    volumes_file_list = glob.glob(os.path.join(volumes_dir, "*.{}".format("npz")))
    dates = [re.split('_|\.', file_name)[-2] for file_name in orderbooks_file_list]
    for date in dates:
        try:
            # get filename for date
            orderbook_file = [file_name for file_name in orderbooks_file_list if date in file_name][0]
            orderflow_file = [file_name for file_name in orderflows_file_list if date in file_name][0]
            volume_file = [file_name for file_name in volumes_file_list if date in file_name][0]
            # load orderbook data
            orderbook_load = pd.read_csv(orderbook_file)
            orderbook_features = orderbook_load.loc[:, [feature for feature in orderbook_load.columns if ('ASK' in feature or 'BID' in feature)]].values
            orderbook_responses = orderbook_load.loc[:, [feature for feature in orderbook_load.columns if not ('ASK' in feature or 'BID' in feature)]].values
            # load orderflow data
            orderflow_load = pd.read_csv(orderflow_file)
            orderflow_features = orderflow_load.loc[:, [feature for feature in orderflow_load.columns if ('ASK' in feature or 'BID' in feature)]].values
            orderflow_responses = orderflow_load.loc[:, [feature for feature in orderflow_load.columns if not ('ASK' in feature or 'BID' in feature)]].values
            # load volume data
            volume_load = np.load(volume_file)
            volume_features, volume_responses = volume_load['features'], volume_load['responses']
            # note the arrays match based on index as follows: 
            # orderflow starts one index after orderbook (as it is a diff), while volume has the same index as orderbook
            # thus drop first lines in orderbooks and volumes
            orderbook_features, orderbook_responses = orderbook_features[1:], orderbook_responses[1:]
            volume_features, volume_responses = volume_features[1:], volume_responses[1:] 
            # sanity check (some numerical error is ok)
            assert(np.allclose(orderbook_responses, orderflow_responses))
            assert(np.allclose(orderbook_responses, volume_responses))
            # save as single .npz file
            output_name = os.path.join(output_path, TICKER + "_" + "data" + "_" + date)
            np.savez(output_name + ".npz", orderbook_features=orderbook_features, orderflow_features=orderflow_features, volume_features=volume_features, responses=orderbook_responses)
        except Exception as e: 
            logs.append('An exception occured, for the following TICKER ' + TICKER + ' and date ' + date + '.')
            logs.append(str(e) + '.')
        else:
            del volume_load, volume_features, volume_responses
            os.remove(orderbook_file)
            os.remove(orderflow_file)
            os.remove(volume_file)

    with open("data/logs/processing_logs.txt", "w") as f:
        for log in logs:
            f.write(log + "\n")

    print("please check processing logs.")

    
if __name__ == "__main__":
    TICKERs = ["LILAK", "QRTEA", "XRAY", "CHTR", "PCAR", "EXC", "AAL", "WBA", "ATVI", "AAPL"]
    for TICKER in TICKERs:
        refactor_data(TICKER)