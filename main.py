#!/usr/bin/env python3

import os
import time
import argparse
import numpy as np
import pandas as pd
from pandas import DataFrame
from dask.distributed import Client, LocalCluster
from dask_jobqueue import SLURMCluster
from EMS.manager import do_on_cluster, get_gbq_credentials
import logging

logging.basicConfig(level=logging.INFO)

# Function that generates data with noise; will use again in later homeworks
def generate_data(nrow: int, ncol: int, seed: int = 0) -> tuple:

    # Set seed
    rng = np.random.default_rng(1 + seed * 10000)  # Ensure the seed is non-zero and spans a large range of values.

    # Create length-n vector u with element equal to (-1)^i/sqrt(n)
    u = np.array([(-1)**i/np.sqrt(nrow) for i in range(nrow)])
    v = np.array([(-1)**(i+1)/np.sqrt(ncol) for i in range(ncol)])

    # Generate signal
    signal = 3 * np.outer(u,v)

    # noise matrix of normal(0,1)
    noise = rng.normal(0,1,(nrow,ncol))/np.sqrt(nrow*ncol)

    # observations matrix
    X = signal + noise

    return X, u, v, signal  # return data


def experiment(*, nrow: int, ncol: int, seed: int) -> DataFrame:
    start_time = time.time()

    X, u_true, v_true, signal_true = generate_data(nrow, ncol, seed=seed)

    # Analyze the data using SVD
    U, S, Vh = np.linalg.svd(X)

    v_est = Vh[0,:]

    # Calculate alignment between v_est and v_true
    v_align = np.inner(v_est,v_true)

    # Save u_est, v_est, u_true, v_true in a CSV file with an index column
    d = {'nrow': nrow, 'ncol': ncol, 'seed': seed, "v_alignment": v_align}
    d.update({f've{i:0>3}': ve for i, ve in enumerate(v_est)})
    df = pd.DataFrame(data=d, index=[0])

    # Print runtime
    logging.info(f"Seed: {seed}; {time.time() - start_time} seconds.")
    return df


def build_params(size: int = 1, su_id: str = 'su_ID') -> dict:

    match size:
        case 1:
            exp = dict(table_name=f'stats285_{su_id}_hw5_{size}_blocks',
                        params=[{
                            'nrow': [1000],
                            'ncol': [1000],
                            'seed': [285]
                        }])
        case _:
            exp = dict(table_name=f'stats285_{su_id}_hw5_{size}_blocks',
                        params=[{
                            'nrow': [1000],
                            'ncol': [1000],
                            'seed': list(range(size))
                        }])
    return exp


def do_cluster_experiment(size: int = 1, su_id: str = 'su_ID', credentials=None):
    exp = build_params(size=size, su_id=su_id)
    with SLURMCluster(cores=8, memory='4GiB', processes=1, walltime='00:15:00') as cluster:
        cluster.scale(8)
        logging.info(cluster.job_script())
        with Client(cluster) as client:
            do_on_cluster(exp, experiment, client, credentials=credentials)
        cluster.scale(0)


def do_local_experiment(size: int = 1, su_id: str = 'su_ID', credentials=None):
    exp = build_params(size=size, su_id=su_id)
    with LocalCluster() as cluster:
        with Client(cluster) as client:
            do_on_cluster(exp, experiment, client, credentials=credentials)


if __name__ == "__main__":
    # Parse the argument passed to this function that is either "local" or "cluster"
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", help="type", type=str, default="local")
    type = parser.parse_args().type

    if type == "local":
        do_local_experiment(size=1000, su_id=f'{os.environ.get("TABLE_NAME", "su_ID")}_slurm_large_node_gbq_2',
                            credentials=get_gbq_credentials('stanford-stats-285-donoho-0dc233389eb9.json'))
    elif type == "cluster":
        do_cluster_experiment(size=1000, su_id=f'{os.environ.get("TABLE_NAME", "su_ID")}_slurm_cluster_2',
                              credentials=get_gbq_credentials('stanford-stats-285-donoho-0dc233389eb9.json'))
