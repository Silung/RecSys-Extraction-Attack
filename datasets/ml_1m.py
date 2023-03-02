from .base import AbstractDataset
from .utils import *

from datetime import date
from pathlib import Path
import pickle
import shutil
import tempfile
import os

import numpy as np
import pandas as pd
from tqdm import tqdm
tqdm.pandas()


class ML1MDataset(AbstractDataset):
    @classmethod
    def code(cls):
        return 'ml-1m'

    '''
    description: 数据集下载链接
    return {str}
    '''
    @classmethod
    def url(cls):
        return 'http://files.grouplens.org/datasets/movielens/ml-1m.zip'

    @classmethod
    def zip_file_content_is_folder(cls):
        return True

    '''
    description: 下载数据文件内的文件列表
    '''
    @classmethod
    def all_raw_file_names(cls):
        return ['README',
                'movies.dat',
                'ratings.dat',
                'users.dat']

    @classmethod
    def is_zipfile(cls):
        return True

    @classmethod
    def is_7zfile(cls):
        return False

    '''
    description: 下载数据，已有数据则跳过
    param {*} self
    return {None} 
    '''
    def maybe_download_raw_dataset(self):
        folder_path = self._get_rawdata_folder_path()
        
        # 检查文件是否已经下载
        if folder_path.is_dir() and\
           all(folder_path.joinpath(filename).is_file() for filename in self.all_raw_file_names()):
            print('Raw data already exists. Skip downloading')
            return
        
        # 下载文件
        print("Raw file doesn't exist. Downloading...")
        tmproot = Path(tempfile.mkdtemp())
        tmpzip = tmproot.joinpath('file.zip')
        tmpfolder = tmproot.joinpath('folder')
        download(self.url(), tmpzip)
        unzip(tmpzip, tmpfolder)
        if self.zip_file_content_is_folder():
            tmpfolder = tmpfolder.joinpath(os.listdir(tmpfolder)[0])
        shutil.move(tmpfolder, folder_path)
        shutil.rmtree(tmproot)
        print()

    '''
    description: 下载原始数据，去除互动较少的user & item，重新生成Index并划分训练集、验证集和测试集，最后将数据和映射关系保存到字典中序列化
    param {*} self
    return {None}
    '''
    def preprocess(self):
        dataset_path = self._get_preprocessed_dataset_path()
        if dataset_path.is_file():
            print('Already preprocessed. Skip preprocessing')
            return
        if not dataset_path.parent.is_dir():
            dataset_path.parent.mkdir(parents=True)
        self.maybe_download_raw_dataset()
        df = self.load_ratings_df()
        df = self.filter_triplets(df)
        df, umap, smap = self.densify_index(df)
        train, val, test = self.split_df(df, len(umap))
        dataset = {'train': train,
                   'val': val,
                   'test': test,
                   'umap': umap,
                   'smap': smap}
        with dataset_path.open('wb') as f:
            pickle.dump(dataset, f)

    '''
    description: 加载movielens-1m数据中的ratings.dat文件，生成user-item-time关系表
    param {*} self
    return {pandas.Dataframe}
    '''
    def load_ratings_df(self) -> pd.DataFrame:
        folder_path = self._get_rawdata_folder_path()
        file_path = folder_path.joinpath('ratings.dat')
        df = pd.read_csv(file_path, sep='::', header=None)
        df.columns = ['uid', 'sid', 'rating', 'timestamp']
        return df
