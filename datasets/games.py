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
import ast
tqdm.pandas()


class GamesDataset(AbstractDataset):
    @classmethod
    def code(cls):
        return 'games'
 
    '''
    description: 数据集下载链接
    return {str}
    '''
    @classmethod
    def url(cls):
        return 'http://snap.stanford.edu/data/amazon/productGraph/categoryFiles/ratings_Video_Games.csv'

    @classmethod
    def zip_file_content_is_folder(cls):
        return True

    '''
    description: 下载数据文件内的文件列表
    '''
    @classmethod
    def all_raw_file_names(cls):
        return ['games.csv']

    @classmethod
    def is_zipfile(cls):
        return False

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
        if folder_path.is_dir() and\
           all(folder_path.joinpath(filename).is_file() for filename in self.all_raw_file_names()):
            print('Raw data already exists. Skip downloading')
            return
        
        print("Raw file doesn't exist. Downloading...")
        download(self.url(), 'file.csv')
        os.mkdir(folder_path)
        shutil.move('file.csv', folder_path.joinpath(self.code() + '.csv'))
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
        else:
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
        
        text_path = self._get_preprocessed_text_path()
        if text_path.is_file():
            print('Text already preprocessed. Skip preprocessing')
        else:
            df_item = self.load_item()
            user_info = None
            item_info = df_item.set_index('sid').to_dict()
            
            text = {'user': user_info,
                    'item': item_info}
            with text_path.open('wb') as f:
                pickle.dump(text, f)
    '''
    description: 生成user-item-rating-time关系表
    param {*} self
    return {pandas.Dataframe}
    '''
    def load_ratings_df(self) -> pd.DataFrame:
        folder_path = self._get_rawdata_folder_path()
        file_path = folder_path.joinpath('games.csv')
        df = pd.read_csv(file_path, header=None)
        df.columns = ['uid', 'sid', 'rating', 'timestamp']
        return df

    def load_item(self) -> pd.DataFrame:
        folder_path = self._get_rawdata_folder_path()
        file_path = folder_path.joinpath('meta_Video_Games.json')
        data = []
        f = open(file_path, 'r', encoding='utf-8')
        for line in f.readlines():
            temp = ast.literal_eval(line)
            if 'title' in temp:
                data.append([temp['asin'], temp['title']])
            elif 'description' in temp:
                data.append([temp['asin'], temp['description'][:100]])
            else:
                data.append([temp['asin'], temp['asin']])

        return pd.DataFrame(data, columns=['sid', 'title'])
