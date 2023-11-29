#!/usr/bin/env python3

import logging
import numpy as np
from sklearn.metrics import accuracy_score
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import train_test_split
import xgboost as xgb
import catboost
import lightgbm as lgb
import pandas as pd
from pandas import DataFrame

from dask.distributed import LocalCluster, Client
from google.cloud import bigquery
from google.oauth2 import service_account
from EMS.manager import EvalOnCluster, get_gbq_credentials, get_dataset

from google.cloud import aiplatform
from google.cloud.aiplatform.vizier import pyvizier as vz
from google.cloud.aiplatform.vizier import Study

logger = logging.getLogger(__name__)


"""
Let’s do these four datasets:
1) Adult Income: https://archive.ics.uci.edu/dataset/2/adult
2) California housing: https://www.kaggle.com/datasets/camnugent/california-housing-prices
3) Forest Covertype: https://archive.ics.uci.edu/dataset/31/covertype
4) Higgs: https://www.kaggle.com/c/higgs-boson
"""
class StudyBOOST:
    XGBOOST = 'xgboost'
    CATBOOST = 'catboost'
    LIGHTGBM = 'lightgbm'


class StudyURL:
    UCIML_ADULT_INCOME = 'https://archive.ics.uci.edu/dataset/2/adult'
    KAGGLE_CALIFORNIA_HOUSING_PRICES = 'https://www.kaggle.com/datasets/camnugent/california-housing-prices'
    UCIML_FOREST_COVERTYPE = 'https://archive.ics.uci.edu/dataset/31/covertype'
    KAGGLE_HIGGS_BOSON = 'https://www.kaggle.com/c/higgs-boson/'
    KAGGLE_HIGGS_BOSON_TRAINING = 'https://www.kaggle.com/c/higgs-boson/training'
    KAGGLE_HIGGS_BOSON_TEST = 'https://www.kaggle.com/c/higgs-boson/test'


TABLE_NAMES = {  # URL to GBQ table name map.
    StudyURL.UCIML_ADULT_INCOME: 'XYZ.adult_income',
    StudyURL.KAGGLE_CALIFORNIA_HOUSING_PRICES: 'XYZ.california_housing_prices',
    StudyURL.UCIML_FOREST_COVERTYPE: 'XYZ.forest_covertype',
    StudyURL.KAGGLE_HIGGS_BOSON_TRAINING: 'XYZ.higgs_boson_training',
    StudyURL.KAGGLE_HIGGS_BOSON_TEST: 'XYZ.higgs_boson_test',
}


def get_df_from_gbq(table_name, credentials: service_account.credentials = None):
    client = bigquery.Client(credentials=credentials)
    query = f"SELECT * FROM `{table_name}`"
    df = client.query(query).to_dataframe()
    return df


def push_tables_to_cluster(tables: dict, c: Client, credentials: service_account.credentials = None):
    for key, table in tables.items():
        df = get_df_from_gbq(table, credentials)
        c.publish_dataset(df, name=key)
        logger.info(f'{key}\n{df}')


# Objective functions to maximize.
def experiment_local(*, url: str, X_df: DataFrame, y_df: DataFrame, boost: str, depth: int, reg_lambda: float, learning_rate: float) -> DataFrame:
    # Create data array
    X = X_df.values

    # Convert y into target array
    y_array = y_df.iloc[:, 0].to_numpy()  # Changed

    # Create target vector
    if np.issubdtype(y_array.dtype, np.number):
        y = y_array
    else:
        # If y is categorical (including strings), use LabelEncoder for encoding
        encoder = LabelEncoder()
        y = encoder.fit_transform(y_array)

    # Split into train and test
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2)

    num_rounds = 100  # Number of boosting rounds
    match boost:
        case StudyBOOST.XGBOOST:
            model = xgb.XGBClassifier(learning_rate=learning_rate,
                                      reg_lambda=reg_lambda,
                                      max_depth=depth)
        case StudyBOOST.CATBOOST:
            model = catboost.CatBoostClassifier(learning_rate=learning_rate,
                                                l2_leaf_reg=reg_lambda,
                                                depth=depth)
        case StudyBOOST.LIGHTGBM:
            model = lgb.LGBMClassifier(learning_rate=learning_rate,
                                       lambda_l2=reg_lambda,
                                       max_depth=depth)
        case _:
            raise Exception("Invalid Method Name!")
    model.fit(X_train, y_train)

    # Make predictions on the test set
    test_preds = model.predict(X_test)
    test_predictions = [1 if x > 0.5 else 0 for x in test_preds]
    test_accuracy = accuracy_score(y_test, test_predictions)

    return DataFrame(data={'url': url, 'boost': boost, 'depth': depth,
                           'reg_lambda': reg_lambda, 'learning_rate': learning_rate,
                           'test_accuracy': test_accuracy},
                     index=[0])


def category_encode(df: DataFrame) -> DataFrame:
    # Select object columns
    object_cols = df.select_dtypes(include='object').columns

    # One-shot encode these columns
    df_encoded = pd.get_dummies(df, columns=object_cols)

    # Preview
    logger.info(f'{df_encoded.head()}')

    return df_encoded


def normalize_dataset(url: str, df: DataFrame) -> (DataFrame, DataFrame):
    match url:
        case StudyURL.UCIML_ADULT_INCOME:
            y_df = df[['income']]
            X_df = df.drop('income', axis=1)
        case StudyURL.KAGGLE_CALIFORNIA_HOUSING_PRICES:
            y_df = df[['median_house_value']]
            X_df = df.drop('median_house_value', axis=1)
        case StudyURL.UCIML_FOREST_COVERTYPE:
            y_df = df[['Cover_Type']]
            X_df = df.drop('Cover_Type', axis=1)
        case StudyURL.KAGGLE_HIGGS_BOSON_TRAINING | StudyURL.KAGGLE_HIGGS_BOSON_TEST:
            y_df = df[['Label']]
            X_df = df.drop('Label', axis = 1)
        case _:
            raise Exception("Invalid Dataset Name!")
    X_df = category_encode(X_df)
    return X_df, y_df


def experiment(*, url: str, boost: str, depth: int, reg_lambda: float, learning_rate: float) -> DataFrame:
    df = get_dataset(url)
    df, y_df = normalize_dataset(url, df)
    return experiment_local(url=url, X_df=df, y_df=y_df, boost=boost,
                            depth=depth, reg_lambda=reg_lambda, learning_rate=learning_rate)


# def experiment_1(*, w: float, x: int, y: float, z: str) -> DataFrame:
#     objective = w**2 - y**2 + x * ord(z)
#     return DataFrame(data={'w': w, 'x': x, 'y': y, 'z': z, 'objective': objective}, index=[0])


def get_vertex_study(study_id: str = 'xyz_example',
                     project: str = 'stanford-stats-285-donoho',
                     credentials: service_account.Credentials = None) -> Study:
    # Algorithm, search space, and metrics.
    study_config = vz.StudyConfig(algorithm=vz.Algorithm.RANDOM_SEARCH)  # Free on Vertex AI.
    # study_config = vz.StudyConfig(algorithm=vz.Algorithm.GAUSSIAN_PROCESS_BANDIT)

    study_config.search_space.root.add_float_param('reg_lambda', 0.0, 5.0)
    study_config.search_space.root.add_float_param('learning_rate', 0.0, 5.0)
    study_config.search_space.root.add_int_param('depth', -2, 2)
    # study_config.search_space.root.add_discrete_param('y', [0.3, 7.2])
    study_config.search_space.root.add_categorical_param('url', [
        StudyURL.UCIML_ADULT_INCOME,
        StudyURL.KAGGLE_CALIFORNIA_HOUSING_PRICES,
        StudyURL.UCIML_FOREST_COVERTYPE,
        StudyURL.KAGGLE_HIGGS_BOSON_TRAINING
    ])
    study_config.search_space.root.add_categorical_param('boost', [
        StudyBOOST.XGBOOST,
        StudyBOOST.CATBOOST,
        StudyBOOST.LIGHTGBM
    ])
    study_config.metric_information.append(vz.MetricInformation('metric_name', goal=vz.ObjectiveMetricGoal.MAXIMIZE))

    aiplatform.init(project=project, location='us-central1', credentials=credentials)
    study = Study.create_or_load(display_name=study_id, problem=study_config)
    return study


def setup_xyz_vertex_on_local_cluster(credentials: service_account.Credentials):
    study = get_vertex_study(study_id='test_cluster_01', credentials=credentials)

    with LocalCluster() as lc, Client(lc) as client:
        push_tables_to_cluster(TABLE_NAMES, client, credentials=credentials)
        ec = EvalOnCluster(client, None)
        # ec = EvalOnCluster(client, 'test_cluster_01')
        in_cluster = {}
        for _ in range(20):
            for suggestion in study.suggest(count=100):
                params = suggestion.materialize().parameters.as_dict()
                params['x'] = round(params['x'])
                key = ec.eval_params(experiment, params)
                in_cluster[key] = suggestion
            for df, key in ec.result():
                measurement = vz.Measurement()
                measurement.metrics['metric_name'] = df.iloc[0]['objective']
                suggestion = in_cluster[key]
                suggestion.add_measurement(measurement=measurement)
                suggestion.complete(measurement=measurement)
                del in_cluster[key]
        ec.final_push()
    optimal_trials = study.optimal_trials()
    logger.info(f'{optimal_trials}')


def create_config() -> dict:
    ems_spec = {
        'params': [{
            'depth': [6, 8, 10],
            'reg_lambda': [0.25, 0.5, 1., 2., 4.],
            'boost': [StudyBOOST.XGBOOST, StudyBOOST.CATBOOST, StudyBOOST.LIGHTGBM],
            'url': [
                StudyURL.UCIML_ADULT_INCOME,
                StudyURL.KAGGLE_CALIFORNIA_HOUSING_PRICES,
                StudyURL.UCIML_FOREST_COVERTYPE,
                StudyURL.KAGGLE_HIGGS_BOSON_TRAINING
            ],
            'learning_rate': [0.1, 5.]
        }],
        'param_types': {
            'depth': 'int',
            'reg_lambda': 'float',
            'boost': 'str',
            'url': 'str',
            'learning_rate': 'float'
        },
        'table_name': 'EMS.su_id_XYZ',
        'GCP_project_id': 'stanford-stats-285-donoho',
        'description': 'Describe what this experiment does for future reference.'
    }
    return ems_spec


def setup_experiment(url: str, boost: str, depth: int, reg_lambda: float, learning_rate: float, credentials: service_account.Credentials):
    df = get_df_from_gbq(TABLE_NAMES[url], credentials=credentials)
    df, y_df = normalize_dataset(url, df)
    df_result = experiment_local(url=url, X_df=df, y_df=y_df, boost=boost,
                                 depth=depth, reg_lambda=reg_lambda, learning_rate=learning_rate)
    logger.info(f'{url} by {boost}\n{df_result}')


if __name__ == "__main__":
    credentials = get_gbq_credentials('stanford-stats-285-donoho-vizier-b8a57b59c6d6.json')
    # setup_xyz_vertex_on_local_cluster(credentials=credentials)
    setup_experiment(StudyURL.UCIML_ADULT_INCOME, StudyBOOST.XGBOOST, 6, 0.25, 0.1, credentials=credentials)
    setup_experiment(StudyURL.UCIML_ADULT_INCOME, StudyBOOST.CATBOOST, 6, 0.25, 0.1, credentials=credentials)
    setup_experiment(StudyURL.UCIML_ADULT_INCOME, StudyBOOST.LIGHTGBM, 6, 0.25, 0.1, credentials=credentials)
    setup_experiment(StudyURL.KAGGLE_CALIFORNIA_HOUSING_PRICES, StudyBOOST.XGBOOST, 6, 0.25, 0.1, credentials=credentials)
    setup_experiment(StudyURL.KAGGLE_CALIFORNIA_HOUSING_PRICES, StudyBOOST.CATBOOST, 6, 0.25, 0.1, credentials=credentials)
    setup_experiment(StudyURL.KAGGLE_CALIFORNIA_HOUSING_PRICES, StudyBOOST.LIGHTGBM, 6, 0.25, 0.1, credentials=credentials)
    setup_experiment(StudyURL.UCIML_FOREST_COVERTYPE, StudyBOOST.XGBOOST, 6, 0.25, 0.1, credentials=credentials)
    setup_experiment(StudyURL.UCIML_FOREST_COVERTYPE, StudyBOOST.CATBOOST, 6, 0.25, 0.1, credentials=credentials)
    setup_experiment(StudyURL.UCIML_FOREST_COVERTYPE, StudyBOOST.LIGHTGBM, 6, 0.25, 0.1, credentials=credentials)
    setup_experiment(StudyURL.KAGGLE_HIGGS_BOSON_TRAINING, StudyBOOST.XGBOOST, 6, 0.25, 0.1, credentials=credentials)
    setup_experiment(StudyURL.KAGGLE_HIGGS_BOSON_TRAINING, StudyBOOST.CATBOOST, 6, 0.25, 0.1, credentials=credentials)
    setup_experiment(StudyURL.KAGGLE_HIGGS_BOSON_TRAINING, StudyBOOST.LIGHTGBM, 6, 0.25, 0.1, credentials=credentials)