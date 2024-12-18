from config import STATE_DICT_KEY, OPTIMIZER_STATE_DICT_KEY
from model import *
from .utils import *
from .loggers import *
from .dataset import *
from .dataloader import *

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

import json
import faiss
import random
import numpy as np
from abc import *
from pathlib import Path

from trainer.agent import *
from datasets import dataset_factory
import time

class NoDataRankDistillationTrainer(metaclass=ABCMeta):
    def __init__(self, args, model_code, model, bb_model, test_loader, export_root, loss='ranking', last_epoch=0, last_accum_iter=0, tau=1., margin_topk=0.5, margin_neg=0.5):
        self.args = args
        self.device = args.device
        self.num_items = args.num_items
        self.max_len = args.bert_max_len
        self.batch_size = args.train_batch_size
        self.mask_prob = args.bert_mask_prob
        self.max_predictions = args.bert_max_predictions
        self.CLOZE_MASK_TOKEN = self.num_items + 1

        self.model = model.to(self.device)
        self.model_code = model_code
        self.bb_model = bb_model.to(self.device)

        self.num_epochs = args.num_epochs
        self.last_epoch = last_epoch
        self.last_accum_iter = last_accum_iter
        self.metric_ks = args.metric_ks
        self.best_metric = args.best_metric
        self.export_root = export_root
        self.log_period_as_iter = args.log_period_as_iter

        self.is_parallel = args.num_gpu > 1
        if self.is_parallel:
            self.model = nn.DataParallel(self.model)

        self.test_loader = test_loader
        self.optimizer = self._create_optimizer()
        if args.enable_lr_schedule:
            if args.enable_lr_warmup:
                self.lr_scheduler = self.get_linear_schedule_with_warmup(
                    self.optimizer, args.warmup_steps, (args.num_generated_seqs // self.batch_size + 1) * self.num_epochs * 2)
            else:
                self.lr_scheduler = optim.lr_scheduler.StepLR(
                    self.optimizer, step_size=args.decay_step, gamma=args.gamma)

        self.loss = loss
        self.tau = tau
        self.margin_topk = margin_topk
        self.margin_neg = margin_neg
        if self.loss == 'kl':
            self.loss_func = nn.KLDivLoss(reduction='batchmean')
        elif self.loss == 'ranking':
            self.loss_func_1 = nn.MarginRankingLoss(margin=self.margin_topk)
            self.loss_func_2 = nn.MarginRankingLoss(margin=self.margin_neg)
        elif self.loss == 'myranking':
            self.loss_func_1 = nn.MarginRankingLoss(margin=self.margin_topk)
            self.loss_func_2 = nn.MarginRankingLoss(margin=self.margin_neg)
        elif self.loss == 'list':
            self.loss_func = ListLoss()
        elif self.loss == 'list+neg':
            self.loss_func_1 = ListLoss()
            self.loss_func_2 = nn.MarginRankingLoss(margin=self.margin_neg)
        elif self.loss == 'kl+ct':
            self.loss_func_1 = nn.KLDivLoss(reduction='batchmean')
            self.loss_func_2 = nn.CrossEntropyLoss(ignore_index=0)
        elif self.loss == 'ce+ranking':
            self.loss_func_1 = nn.CrossEntropyLoss()
            self.loss_func_2 = nn.MarginRankingLoss(margin=self.margin_topk)
            self.loss_func_3 = nn.MarginRankingLoss(margin=self.margin_neg)

    def calculate_loss(self, seqs, labels, candidates, lengths=None, gts=None):
        if isinstance(self.model, BERT) or isinstance(self.model, SASRec):
            logits = self.model(seqs)[:, -1, :]
        elif isinstance(self.model, NARM) or isinstance(self.model, GRU4REC):
            logits = self.model(seqs, lengths)
        
        if self.loss == 'kl':
            logits = torch.gather(logits, -1, candidates)
            logits = logits.view(-1, logits.size(-1))
            labels = labels.view(-1, labels.size(-1))
            loss = self.loss_func(F.log_softmax(logits/self.tau, dim=-1), F.softmax(labels/self.tau, dim=-1))
        
        elif self.loss == 'ranking':
            # logits = F.softmax(logits/self.tau, dim=-1)
            weight = torch.ones_like(logits).to(self.device)
            weight[torch.arange(weight.size(0)).unsqueeze(1), candidates] = 0
            neg_samples = torch.distributions.Categorical(F.softmax(weight, -1)).sample_n(candidates.size(-1)).permute(1, 0)
            # assume candidates are in descending order w.r.t. true label
            neg_logits = torch.gather(logits, -1, neg_samples)
            logits = torch.gather(logits, -1, candidates)
            logits_1 = logits[:, :-1].reshape(-1)
            logits_2 = logits[:, 1:].reshape(-1)
            loss = self.loss_func_1(logits_1, logits_2, torch.ones(logits_1.shape).to(self.device))
            loss += self.loss_func_2(logits, neg_logits, torch.ones(logits.shape).to(self.device))
            
        elif self.loss == 'ce+ranking':
            if self.epoch > 20:
                weight = torch.ones_like(logits).to(self.device)
                # logits = F.softmax(logits/self.tau, dim=-1)
                weight[torch.arange(weight.size(0)).unsqueeze(1), candidates] = 0
                neg_samples = torch.distributions.Categorical(F.softmax(weight, -1)).sample_n(candidates.size(-1)).permute(1, 0)
                # assume candidates are in descending order w.r.t. true label
                neg_logits = torch.gather(logits, -1, neg_samples)
                logits = torch.gather(logits, -1, candidates)
                logits_1 = logits[:, :-1].reshape(-1)
                logits_2 = logits[:, 1:].reshape(-1)
                loss = self.loss_func_2(logits_1, logits_2, torch.ones(logits_1.shape).to(self.device))
                loss += self.loss_func_3(logits, neg_logits, torch.ones(logits.shape).to(self.device))
            else:
                # CE
                # logits = torch.gather(logits, -1, candidates)
                logits = logits.view(-1, logits.size(-1))
                loss = self.loss_func_1(logits, gts.reshape(-1))
        
        elif self.loss == 'myranking':
            weight = torch.ones_like(logits).to(self.device)
            weight[torch.arange(weight.size(0)).unsqueeze(1), candidates] = 0
            neg_samples = torch.distributions.Categorical(F.softmax(weight, -1)).sample_n(candidates.size(-1)).permute(1, 0)
            # assume candidates are in descending order w.r.t. true label
            neg_logits = torch.gather(logits, -1, neg_samples)
            logits = torch.gather(logits, -1, candidates)
            loss = self.loss_func_2(logits, neg_logits, torch.ones(logits.shape).to(self.device))
            # for p in range(1,11):
            p = 5
            logits_1 = logits[:, :-p].reshape(-1)
            logits_2 = logits[:, p:].reshape(-1)
            loss += self.loss_func_1(logits_1, logits_2, torch.ones(logits_1.shape).to(self.device))

        elif self.loss == 'list':
            logits = torch.gather(logits, -1, candidates)
            logits = logits.view(-1, logits.size(-1))
            loss = self.loss_func(F.softmax(logits/self.tau, dim=-1))

        elif self.loss == 'list+neg':
            weight = torch.ones_like(logits).to(self.device)
            weight[torch.arange(weight.size(0)).unsqueeze(1), candidates] = 0
            neg_samples = torch.distributions.Categorical(F.softmax(weight, -1)).sample_n(candidates.size(-1)).permute(1, 0)
            neg_logits = torch.gather(logits, -1, neg_samples)
            logits = torch.gather(logits, -1, candidates)
            logits = logits.view(-1, logits.size(-1))
            loss = self.loss_func_1(F.softmax(logits/self.tau, dim=-1))
            loss += self.loss_func_2(logits, neg_logits, torch.ones(logits.shape).to(self.device))
            
        elif self.loss == 'kl+ct':
            logits = torch.gather(logits, -1, candidates)
            logits = logits.view(-1, logits.size(-1))
            labels = labels.view(-1, labels.size(-1))
            loss = self.loss_func_1(F.log_softmax(logits/self.tau, dim=-1), F.softmax(labels/self.tau, dim=-1))
            loss += self.loss_func_2(F.softmax(logits), torch.argmax(labels, dim=-1))
        return loss

    def calculate_metrics(self, batch, similarity=False):
        self.model.eval()
        self.bb_model.eval()

        if isinstance(self.model, BERT) or isinstance(self.model, SASRec):
            seqs, candidates, labels = batch
            seqs, candidates, labels = seqs.to(self.device), candidates.to(self.device), labels.to(self.device)
            scores = self.model(seqs)[:, -1, :]
            metrics = recalls_and_ndcgs_for_ks(scores.gather(1, candidates), labels, self.metric_ks)
        elif isinstance(self.model, NARM) or isinstance(self.model, GRU4REC):
            seqs, lengths, candidates, labels = batch
            seqs, candidates, labels = seqs.to(self.device), candidates.to(self.device), labels.to(self.device)
            lengths = lengths.flatten()
            scores = self.model(seqs, lengths)
            metrics = recalls_and_ndcgs_for_ks(scores.gather(1, candidates), labels, self.metric_ks)

        if similarity:
            if isinstance(self.model, BERT) and isinstance(self.bb_model, BERT):
                soft_labels = self.bb_model(seqs)[:, -1, :]
            elif isinstance(self.model, BERT) and isinstance(self.bb_model, SASRec):
                temp_seqs = torch.cat((torch.zeros(seqs.size(0)).long().unsqueeze(1).to(self.device), seqs[:, :-1]), dim=1)
                soft_labels = self.bb_model(temp_seqs)[:, -1, :]
            elif isinstance(self.model, BERT) and isinstance(self.bb_model, NARM):
                temp_seqs = torch.cat((torch.zeros(seqs.size(0)).long().unsqueeze(1).to(self.device), seqs[:, :-1]), dim=1)
                temp_seqs = self.pre2post_padding(temp_seqs)
                temp_lengths = (temp_seqs > 0).sum(-1).cpu().flatten()
                soft_labels = self.bb_model(temp_seqs, temp_lengths)
            elif isinstance(self.model, SASRec) and isinstance(self.bb_model, SASRec):
                soft_labels = self.bb_model(seqs)[:, -1, :]
            elif isinstance(self.model, SASRec) and isinstance(self.bb_model, BERT):
                temp_seqs = torch.cat((seqs[:, 1:], torch.tensor([self.CLOZE_MASK_TOKEN] * seqs.size(0)).unsqueeze(1).to(self.device)), dim=1)
                soft_labels = self.bb_model(temp_seqs)[:, -1, :]
            elif isinstance(self.model, SASRec) and isinstance(self.bb_model, NARM):
                temp_seqs = self.pre2post_padding(seqs)
                temp_lengths = (temp_seqs > 0).sum(-1).cpu().flatten()
                soft_labels = self.bb_model(temp_seqs, temp_lengths)
            elif isinstance(self.model, NARM) and isinstance(self.bb_model, NARM):
                soft_labels = self.bb_model(seqs, lengths)
            elif isinstance(self.model, NARM) and isinstance(self.bb_model, BERT):
                temp_seqs = self.post2pre_padding(seqs)
                temp_seqs = torch.cat((temp_seqs[:, 1:], torch.tensor([self.CLOZE_MASK_TOKEN] * seqs.size(0)).unsqueeze(1).to(self.device)), dim=1)
                soft_labels = self.bb_model(temp_seqs)[:, -1, :]
            elif isinstance(self.model, NARM) and isinstance(self.bb_model, SASRec):
                temp_seqs = self.post2pre_padding(seqs)
                soft_labels = self.bb_model(temp_seqs)[:, -1, :]
            elif isinstance(self.model, GRU4REC) and isinstance(self.bb_model, GRU4REC):
                soft_labels = self.bb_model(seqs, lengths)

            similarity = kl_agreements_and_intersctions_for_ks(scores, soft_labels, self.metric_ks)
            metrics = {**metrics, **similarity} 
        
        return metrics

    # def generate_random_data(self, k=100, batch_size=50):
    #     dataset = dis_dataset_factory(self.args, self.model_code, self.args.generated_sampler)
    #     if dataset.check_data_present():
    #         print('Dataset already exists. Skip generation')
    #         return

    #     print('Generating dataset...')

    #     batch_tokens = np.zeros((self.args.num_generated_seqs, self.max_len), dtype=int)
    #     for i in range(self.args.num_generated_seqs):
    #         # 生成一个从1到self.num_items的随机排列
    #         batch_tokens[i] = np.random.permutation(np.arange(self.num_items))[:self.max_len] + 1
        
    #     batch_logits = np.array([])
    #     batch_candidates = np.array([])
        
    #     dataset.save_dataset(batch_tokens.tolist(), batch_logits.tolist(), batch_candidates.tolist())

    def generate_data(self, k=100, batch_size=50):
        dataset = dis_dataset_factory(self.args, self.model_code, self.args.generated_sampler)
        if self.args.generated_sampler == 'self' or self.args.few_shot != 0:
            org_dataset = dataset_factory(self.args)
            org_dataset = org_dataset.load_dataset()
            org_data = org_dataset['train']
            org_data = [torch.tensor(v) for v in org_data.values()]
            random.shuffle(org_data)
            
            def pad_tensor(tensor, length):
                repeat_times = length // tensor.size(0) + 1 # 计算需要重复的次数
                padded_tensor = tensor.repeat(repeat_times)[:length]  # 重复并裁剪到目标长度
                return padded_tensor
            
            org_data = torch.stack([pad_tensor(t, self.max_len + 1) for t in org_data])
            
        if dataset.check_data_present():
            print('Dataset already exists. Skip generation')
            return

        gen_step = 1
        offset = 0
        if self.args.generated_sampler == 'llm':
            agent = Agent(self.args)
        elif self.args.generated_sampler == 'llm_pfl':
            agent = ProfileAgent(self.args)
        elif self.args.generated_sampler == 'llm_exam':
            agent = ExampleAgent(self.args)
        elif self.args.generated_sampler in ['llm_seq', 'llm_seq_test', 'mix']:
            agent = SeqAgent(self.args)
            if self.args.dataset_code == 'ml-1m':
                gen_step = 10
            elif self.args.dataset_code in ['beauty', 'steam']:
                gen_step = 5
        else:
            agent = None
            print(f'generated_sampler: {self.args.generated_sampler}')
        
        batch_num = self.args.num_generated_seqs // batch_size
        print('Generating dataset...')
        min_si, max_si = -1, 1
        for i in tqdm(range(batch_num)):
            logits = None
            candidates = None
            temp_seq = [[]]

            if self.args.generated_sampler == 'random':
                # random_tokens = np.zeros((batch_size, self.max_len), dtype=int)
                # for idx in range(batch_size):
                #     # 生成一个从1到self.num_items的随机排列
                #     random_tokens[idx] = np.random.permutation(np.arange(self.num_items))[:self.max_len] + 1
                # random_tokens = torch.tensor(random_tokens)
                random_tokens = torch.randint(low=1, high=self.num_items+1, size=(batch_size, self.max_len), dtype=torch.int)
            if self.args.few_shot > 0:
                rand_base = random.randint(0, self.max_len - self.args.few_shot)
                seqs = org_data[i * batch_size:(i+1) * batch_size, rand_base:rand_base+1].to(self.device)
                if agent is not None:
                    agent.set_history(org_data[i * batch_size:(i+1) * batch_size, rand_base:rand_base+self.args.few_shot])
            elif self.args.generated_sampler == 'self':
                seqs = org_data[i * batch_size:(i+1) * batch_size, 0:1].to(self.device)
            else:
                # TODO 这里可能有问题
                seqs = torch.randint(1, self.num_items + 1, (batch_size, 1)).to(self.device)

            # print(f'first item: {seqs[0]}')
            
            # j_start = len(seqs[0]) - 1
            j_start = 0
            if 'gpt' in self.args.llm:
                if not os.path.exists('batch_log'):
                    os.makedirs('batch_log')
                # temp_file_path = os.path.join("batch_log", f"{self.args.dataset_code}_{self.args.bb_model_code}_batch{i}.pkl")
                # if self.args.resume and os.path.isfile(temp_file_path):
                #     with open(temp_file_path, 'rb') as f:
                #         temp = pickle.load(f)
                #     seqs = temp['seqs']
                #     logits = temp['logits']
                #     candidates = temp['candidates']
                #     j_start = temp['j'] + 1
                #     temp_seq = temp['temp_seq']
            
            self.bb_model.eval()
            with torch.no_grad():
                if isinstance(self.bb_model, BERT):
                    mask_items = torch.tensor([self.CLOZE_MASK_TOKEN] * seqs.size(0)).to(self.device)
                    for j in range(j_start, self.max_len - 1):
                        # if agent is not None and j != 0 and j % 30 == 0:
                        #     agent.update_profiles(list(range(i, self.args.num_generated_seqs if (i+1) * batch_size > self.args.num_generated_seqs else (i+1) * batch_size)), batch_size)
                        if j == 2 and agent is not None:
                            agent.init_profile(seqs)
                        input_seqs = torch.zeros((seqs.size(0), self.max_len)).to(self.device)
                        input_seqs[:, (self.max_len-2-j):-1] = seqs
                        input_seqs[:, -1] = mask_items
                        labels = self.bb_model(input_seqs.long())[:, -1, :]

                        _, sorted_items = torch.sort(labels[:, 1:-1], dim=-1, descending=True)
                        
                        sorted_items_k = sorted_items[:, :k] + 1
                        
                        randomized_label = torch.rand(sorted_items_k.shape).to(self.device)
                        randomized_label = randomized_label / randomized_label.sum(dim=-1).unsqueeze(-1)
                        randomized_label, _ = torch.sort(randomized_label, dim=-1, descending=True)
                        
                        if gen_step != 1 and len(temp_seq[0]) != 0:
                            pass
                        elif j < self.args.few_shot - 1:
                            pass
                        elif agent is not None:
                            # if k > 10:
                            #     idx = torch.randperm(sorted_items_k.shape[1])
                            #     sorted_items_k = sorted_items_k[:, idx].view(sorted_items_k.size())
                            #     sorted_items = sorted_items_k[:,:10]
                            # else:
                                # sorted_items = sorted_items_k
                            sorted_items = []
                            t_sorted_items_k = sorted_items_k.cpu().numpy()
                            t_seqs = seqs.cpu().numpy()
                            for w, items in enumerate(t_sorted_items_k):
                                if self.args.generated_sampler == 'mix':
                                    items_list = list((set(items) | {random.randint(1, self.num_items + 1) for _ in range(len(items))}) - set(t_seqs[w]))
                                    items_list = items_list[:k]
                                else:
                                    items_list = list(set(items) - set(t_seqs[w]))
                                # if self.args.completion:
                                while len(items_list) < k:
                                    rd_item = random.randint(1, self.num_items)
                                    if rd_item in items_list or rd_item in t_seqs[w]:
                                        continue
                                    else:
                                        items_list.append(rd_item)
                                items_list = items_list[:k]
                                # elif len(items_list) == 0:
                                #     items_list = list(items)
                                random.shuffle(items_list)
                                sorted_items.append(items_list)
                            # sorted_items = torch.tensor(sorted_items)
                            selected_indices = agent(sorted_items)
                            # randomized_label = torch.ones_like(sorted_items_k).to(self.device)
                        elif self.args.generated_sampler == 'random':
                            pass
                            # randomized_label = torch.ones_like(sorted_items_k).to(self.device)                        
                        else:
                            selected_indices = torch.distributions.Categorical(F.softmax(torch.ones_like(randomized_label), -1).to(randomized_label.device)).sample()
                        
                        if j < self.args.few_shot - 1:
                            seqs = torch.cat((seqs, org_data[i * batch_size:(i+1) * batch_size][:, rand_base+j+1:rand_base+j+2].to(self.device)), 1)
                        else:
                            if gen_step != 1:
                                if len(temp_seq[0]) == 0:
                                    row_indices = torch.tensor([[i]*gen_step for i in range(sorted_items_k.size(0))])
                                    temp_seq = sorted_items_k[row_indices, selected_indices].to(self.device)
                                seqs = torch.cat((seqs, temp_seq[:, :1]), 1)
                                temp_seq = temp_seq[:, 1:]
                            elif self.args.generated_sampler == 'random':
                                seqs = torch.cat((seqs, random_tokens[:, j].unsqueeze(1).to(self.device)), 1)
                            elif self.args.generated_sampler == 'self':
                                seqs = torch.cat((seqs, org_data[i * batch_size:(i+1) * batch_size][:, j+1:j+2].to(self.device)), 1)
                            else:
                                row_indices = torch.arange(sorted_items_k.size(0))
                                seqs = torch.cat((seqs, sorted_items_k[row_indices, selected_indices].unsqueeze(1).to(self.device)), 1)

                        try:
                            logits = torch.cat((logits, randomized_label.unsqueeze(1)), 1)
                            candidates = torch.cat((candidates, sorted_items_k.unsqueeze(1)), 1)
                        except:
                            logits = randomized_label.unsqueeze(1)
                            candidates = sorted_items_k.unsqueeze(1)
                        # print(f'seqs[0]: {seqs[0]}')
                        if 'gpt' in self.args.llm and not self.args.debug:
                            if not os.path.exists('batch_log'):
                                os.makedirs('batch_log')
                            temp_file_path = os.path.join("batch_log", f"{self.args.dataset_code}_{self.args.bb_model_code}_batch{i}.pkl")
                            print(f'\rlen of seqs[0]:{len(seqs[0])}')
                            temp = {'seqs': seqs,
                                'logits': logits,
                                'candidates': candidates,
                                'j':j,
                                'temp_seq':temp_seq}
                    
                            with open(temp_file_path, 'wb') as f:
                                pickle.dump(temp, f)
                    
                    input_seqs = torch.zeros((seqs.size(0), self.max_len)).to(self.device)
                    input_seqs[:, :-1] = seqs[:, -(self.max_len-1):]
                    input_seqs[:, -1] = mask_items
                    labels = self.bb_model(input_seqs.long())[:, -1, :]
                    _, sorted_items = torch.sort(labels[:, 1:-1], dim=-1, descending=True)
                    sorted_items = sorted_items[:, :k] + 1
                    randomized_label = torch.rand(sorted_items.shape).to(self.device)
                    randomized_label = randomized_label / randomized_label.sum(dim=-1).unsqueeze(-1)
                    randomized_label, _ = torch.sort(randomized_label, dim=-1, descending=True)
                    
                    # print(logits.shape)
                    # print(randomized_label.shape)
                    logits = torch.cat((logits, randomized_label.unsqueeze(1)), 1)
                    candidates = torch.cat((candidates, sorted_items.unsqueeze(1)), 1)

                elif isinstance(self.bb_model, SASRec):
                    for j in range(j_start, self.max_len - 1):
                        # if agent is not None and j != 0 and j % 30 == 0:
                        #     agent.update_profiles(list(range(i, self.args.num_generated_seqs if (i+1) * batch_size > self.args.num_generated_seqs else (i+1) * batch_size)), batch_size)
                        if j == 2 and agent is not None:
                            agent.init_profile(seqs)
                        input_seqs = torch.zeros((seqs.size(0), self.max_len)).to(self.device)
                        input_seqs[:, (self.max_len-1-j):] = seqs
                        labels = self.bb_model(input_seqs.long())[:, -1, :]

                        _, sorted_items = torch.sort(labels[:, 1:], dim=-1, descending=True)
                        sorted_items_k = sorted_items[:, :k] + 1
                        
                        randomized_label = torch.rand(sorted_items_k.shape).to(self.device)
                        randomized_label = randomized_label / randomized_label.sum(dim=-1).unsqueeze(-1)
                        randomized_label, _ = torch.sort(randomized_label, dim=-1, descending=True)
                        
                        if gen_step != 1 and len(temp_seq[0]) != 0:
                            pass
                        elif j < self.args.few_shot - 1:
                            pass
                        elif agent is not None:
                            sorted_items = []
                            t_sorted_items_k = sorted_items_k.cpu().numpy()
                            t_seqs = seqs.cpu().numpy()
                            for w, items in enumerate(t_sorted_items_k):
                                if self.args.generated_sampler == 'mix':
                                    items_list = list((set(items) | {random.randint(1, self.num_items + 1) for _ in range(len(items))}) - set(t_seqs[w]))
                                    items_list = items_list[:k]
                                else:
                                    items_list = list(set(items) - set(t_seqs[w]))
                                # if self.args.completion:
                                while len(items_list) < k:
                                    rd_item = random.randint(1, self.num_items)
                                    if rd_item in items_list or rd_item in t_seqs[w]:
                                        continue
                                    else:
                                        items_list.append(rd_item)
                                items_list = items_list[:k]
                                # elif len(items_list) == 0:
                                #     items_list = list(items)
                                random.shuffle(items_list)
                                sorted_items.append(items_list)
                            # sorted_items = torch.tensor(sorted_items)
                            selected_indices = agent(sorted_items)
                            # randomized_label = torch.ones_like(sorted_items_k).to(self.device)
                        elif self.args.generated_sampler == 'random':
                            pass
                        else:                           
                            selected_indices = torch.distributions.Categorical(F.softmax(torch.ones_like(randomized_label), -1).to(randomized_label.device)).sample()
                        
                        if j < self.args.few_shot - 1:
                            seqs = torch.cat((seqs, org_data[i * batch_size:(i+1) * batch_size][:, rand_base+j+1:rand_base+j+2].to(self.device)), 1)
                        else:
                            if gen_step != 1:
                                if len(temp_seq[0]) == 0:
                                    row_indices = torch.tensor([[i]*gen_step for i in range(sorted_items_k.size(0))])
                                    temp_seq = sorted_items_k[row_indices, selected_indices].to(self.device)
                                seqs = torch.cat((seqs, temp_seq[:, :1]), 1)
                                temp_seq = temp_seq[:, 1:]
                            elif self.args.generated_sampler == 'random':
                                seqs = torch.cat((seqs, random_tokens[:, j].unsqueeze(1).to(self.device)), 1)
                            elif self.args.generated_sampler == 'self':
                                seqs = torch.cat((seqs, org_data[i * batch_size:(i+1) * batch_size][:, j+1:j+2].to(self.device)), 1)
                            else:
                                row_indices = torch.arange(sorted_items_k.size(0))
                                seqs = torch.cat((seqs, sorted_items_k[row_indices, selected_indices].unsqueeze(1).to(self.device)), 1)

                        try:
                            logits = torch.cat((logits, randomized_label.unsqueeze(1)), 1)
                            candidates = torch.cat((candidates, sorted_items_k.unsqueeze(1)), 1)
                        except:
                            logits = randomized_label.unsqueeze(1)
                            candidates = sorted_items_k.unsqueeze(1)
                            
                        if 'gpt' in self.args.llm and not self.args.debug:
                            if not os.path.exists('batch_log'):
                                os.makedirs('batch_log')
                            temp_file_path = os.path.join("batch_log", f"{self.args.dataset_code}_{self.args.bb_model_code}_batch{i}.pkl")
                            temp = {'seqs': seqs,
                                'logits': logits,
                                'candidates': candidates,
                                'j':j,
                                'temp_seq':temp_seq}
                    
                            with open(temp_file_path, 'wb') as f:
                                pickle.dump(temp, f)

                    labels = self.bb_model(seqs.long())[:, -1, :]
                    _, sorted_items = torch.sort(labels[:, 1:], dim=-1, descending=True)
                    sorted_items_k = sorted_items[:, :k] + 1
                    randomized_label = torch.rand(sorted_items_k.shape).to(self.device)
                    randomized_label = randomized_label / randomized_label.sum(dim=-1).unsqueeze(-1)
                    randomized_label, _ = torch.sort(randomized_label, dim=-1, descending=True)
                    
                    logits = torch.cat((logits, randomized_label.unsqueeze(1)), 1)
                    candidates = torch.cat((candidates, sorted_items_k.unsqueeze(1)), 1)

                elif isinstance(self.bb_model, NARM) or isinstance(self.bb_model, GRU4REC):
                    for j in range(j_start, self.max_len - 1):
                        if j == 2 and agent is not None:
                            agent.init_profile(seqs)
                        lengths = torch.tensor([j + 1] * seqs.size(0))
                        labels = self.bb_model(seqs.long(), lengths)

                        _, sorted_items = torch.sort(labels[:, 1:], dim=-1, descending=True)
                        sorted_items_k = sorted_items[:, :k] + 1
                        
                        randomized_label = torch.rand(sorted_items_k.shape).to(self.device)
                        randomized_label = randomized_label / randomized_label.sum(dim=-1).unsqueeze(-1)
                        randomized_label, _ = torch.sort(randomized_label, dim=-1, descending=True)
                        
                        if gen_step != 1 and len(temp_seq[0]) != 0:
                            pass
                        elif j < self.args.few_shot - 1:
                            pass
                        elif agent is not None:
                            sorted_items = []
                            t_sorted_items_k = sorted_items_k.cpu().numpy()
                            t_seqs = seqs.cpu().numpy()
                            for w, items in enumerate(t_sorted_items_k):
                                if self.args.generated_sampler == 'mix':
                                    items_list = list((set(items) | {random.randint(1, self.num_items + 1) for _ in range(len(items))}) - set(t_seqs[w]))
                                    items_list = items_list[:k]
                                else:
                                    items_list = list(set(items) - set(t_seqs[w]))
                                # if self.args.completion:
                                while len(items_list) < k:
                                    rd_item = random.randint(1, self.num_items)
                                    if rd_item in items_list or rd_item in t_seqs[w]:
                                        continue
                                    else:
                                        items_list.append(rd_item)
                                items_list = items_list[:k]
                                # elif len(items_list) == 0:
                                #     items_list = list(items)
                                random.shuffle(items_list)
                                sorted_items.append(items_list)
                            selected_indices = agent(sorted_items)
                        elif self.args.generated_sampler == 'random':
                            pass
                        else:
                            selected_indices = torch.distributions.Categorical(F.softmax(torch.ones_like(randomized_label), -1).to(randomized_label.device)).sample()
                        
                        if j < self.args.few_shot - 1:
                            seqs = torch.cat((seqs, org_data[i * batch_size:(i+1) * batch_size][:, rand_base+j+1:rand_base+j+2].to(self.device)), 1)
                        else:
                            if gen_step != 1:
                                if len(temp_seq[0]) == 0:
                                    row_indices = torch.tensor([[i]*gen_step for i in range(sorted_items_k.size(0))])
                                    temp_seq = sorted_items_k[row_indices, selected_indices].to(self.device)
                                seqs = torch.cat((seqs, temp_seq[:, :1]), 1)
                                temp_seq = temp_seq[:, 1:]
                            elif self.args.generated_sampler == 'random':
                                seqs = torch.cat((seqs, random_tokens[:, j].unsqueeze(1).to(self.device)), 1)
                            elif self.args.generated_sampler == 'self':
                                seqs = torch.cat((seqs, org_data[i * batch_size:(i+1) * batch_size][:, j+1:j+2].to(self.device)), 1)
                            else:
                                row_indices = torch.arange(sorted_items_k.size(0))
                                seqs = torch.cat((seqs, sorted_items_k[row_indices, selected_indices].unsqueeze(1).to(self.device)), 1)
                        
                        try:
                            logits = torch.cat((logits, randomized_label.unsqueeze(1)), 1)
                            candidates = torch.cat((candidates, sorted_items_k.unsqueeze(1)), 1)
                        except:
                            logits = randomized_label.unsqueeze(1)
                            candidates = sorted_items_k.unsqueeze(1)
                            
                        if 'gpt' in self.args.llm and not self.args.debug:
                            if not os.path.exists('batch_log'):
                                os.makedirs('batch_log')
                            temp_file_path = os.path.join("batch_log", f"{self.args.dataset_code}_{self.args.bb_model_code}_batch{i}.pkl")
                            print(f'\rlen of seqs[0]:{len(seqs[0])}')
                            temp = {'seqs': seqs,
                                'logits': logits,
                                'candidates': candidates, 
                                'j':j,
                                'temp_seq':temp_seq}
                    
                            with open(temp_file_path, 'wb') as f:
                                pickle.dump(temp, f)
                        
                    lengths = torch.tensor([self.max_len] * seqs.size(0))
                    labels = self.bb_model(seqs.long(), lengths)
                    # labels2 = self.bb_model(seqs[:,190:].long(), lengths-190)
                    # si = F.cosine_similarity(labels, labels2, dim=1)
                    _, sorted_items = torch.sort(labels[:, 1:], dim=-1, descending=True)
                    sorted_items_k = sorted_items[:, :k] + 1
                    randomized_label = torch.rand(sorted_items_k.shape).to(self.device)
                    randomized_label = randomized_label / randomized_label.sum(dim=-1).unsqueeze(-1)
                    randomized_label, _ = torch.sort(randomized_label, dim=-1, descending=True)
                    logits = torch.cat((logits, randomized_label.unsqueeze(1)), 1)
                    candidates = torch.cat((candidates, sorted_items_k.unsqueeze(1)), 1)

                # if self.args.generated_sampler == 'self':
                #     gt = torch.cat((seqs, org_data[i * batch_size:(i+1) * batch_size][:, self.max_len:self.max_len+1].to(self.device)), 1)[:, 1:]
                # gt = seqs[:, 1:]
                gt = torch.tensor([])

                if i == 0:
                    batch_tokens = seqs.cpu().numpy()
                    batch_logits = logits.cpu().numpy()
                    batch_candidates = candidates.cpu().numpy()
                    gts = gt.cpu().numpy()
                else:
                    batch_tokens = np.concatenate((batch_tokens, seqs.cpu().numpy()))
                    batch_logits = np.concatenate((batch_logits, logits.cpu().numpy()))
                    batch_candidates = np.concatenate((batch_candidates, candidates.cpu().numpy()))
                    gts = np.concatenate((gts, gt.cpu().numpy()))

        dataset.save_dataset(batch_tokens.tolist(), batch_logits.tolist(), batch_candidates.tolist(), gts.tolist())

    def train(self):        
        accum_iter = self.last_accum_iter
        self.writer, self.train_loggers, self.val_loggers = self._create_loggers()
        self.logger_service = LoggerService(
            self.train_loggers, self.val_loggers)
        self.generate_data(k=self.args.k, batch_size=self.args.batch_size)
        if self.args.gen_data_only:
            return
        dis_train_loader, dis_val_loader = dis_train_loader_factory(self.args, self.model_code, self.args.generated_sampler)
        print(f'## Distilling model via {self.args.generated_sampler} data... ##')
        # self.validate(dis_val_loader, 0, accum_iter)
        for epoch in range(self.last_epoch, self.num_epochs):
            accum_iter = self.train_one_epoch(epoch, accum_iter, dis_train_loader, dis_val_loader, stage=1)
            if self.args.debug and epoch != 0 and epoch % 10 == 0:
                print(self.test())
        metrics = self.test()
        
        self.logger_service.complete({
            'state_dict': (self._create_state_dict()),
        })
        self.writer.close()

        return metrics

    def train_one_epoch(self, epoch, accum_iter, train_loader, val_loader, stage=0):
        self.epoch = epoch
        self.model.train()
        self.bb_model.eval()
        average_meter_set = AverageMeterSet()
        
        tqdm_dataloader = tqdm(train_loader)
        for batch_idx, batch in enumerate(tqdm_dataloader):
            self.optimizer.zero_grad()
            if isinstance(self.model, BERT) or isinstance(self.model, SASRec):
                seqs, candidates, labels, gts = batch
                seqs, candidates, labels, gts = seqs.to(self.device), candidates.to(self.device), labels.to(self.device), gts.to(self.device)
                loss = self.calculate_loss(seqs, labels, candidates, gts=gts)
            elif isinstance(self.model, NARM) or isinstance(self.model, GRU4REC):
                seqs, lengths, candidates, labels, gts = batch
                if self.args.noise:
                    noise_p = torch.rand(seqs.shape)
                    seqs_noise = torch.randint_like(seqs, 1, self.args.num_items + 1)
                    seqs = torch.where(noise_p < 0.1, seqs_noise, seqs)
                lengths = lengths.flatten()
                seqs, candidates, labels, gts = seqs.to(self.device), candidates.to(self.device), labels.to(self.device), gts.to(self.device)
                loss = self.calculate_loss(seqs, labels, candidates, lengths=lengths, gts=gts)
            
            loss.backward()
            self.clip_gradients(5)
            self.optimizer.step()
            accum_iter += int(seqs.size(0))
            average_meter_set.update('loss', loss.item())
            tqdm_dataloader.set_description(
                'Epoch {} Stage {}, loss {:.3f} '.format(epoch+1, stage, average_meter_set['loss'].avg))

            if self._needs_to_log(accum_iter):
                log_data = {
                    'state_dict': (self._create_state_dict()),
                    'epoch': epoch+1,
                    'accum_iter': accum_iter,
                }
                log_data.update(average_meter_set.averages())
                self.logger_service.log_train(log_data)
            
            if self.args.enable_lr_schedule:
                self.lr_scheduler.step()
        
        self.validate(val_loader, epoch, accum_iter)
        return accum_iter

    def validate(self, val_loader, epoch, accum_iter):
        self.model.eval()
        average_meter_set = AverageMeterSet()
        with torch.no_grad():
            tqdm_dataloader = tqdm(val_loader)
            for batch_idx, batch in enumerate(tqdm_dataloader):
                metrics = self.calculate_metrics(batch)
                self._update_meter_set(average_meter_set, metrics)
                self._update_dataloader_metrics(
                    tqdm_dataloader, average_meter_set)

            log_data = {
                'state_dict': (self._create_state_dict()),
                'epoch': epoch+1,
                'accum_iter': accum_iter,
            }
            log_data.update(average_meter_set.averages())
            self.logger_service.log_val(log_data)

    def test(self):
        wb_model = torch.load(os.path.join(
            self.export_root, 'models', f'best_acc{self.args.id}_model.pth')).get(STATE_DICT_KEY)
        self.model.load_state_dict(wb_model)
        
        self.model.eval()
        self.bb_model.eval()
        average_meter_set = AverageMeterSet()
        with torch.no_grad():
            tqdm_dataloader = tqdm(self.test_loader)
            for batch_idx, batch in enumerate(tqdm_dataloader):
                metrics = self.calculate_metrics(batch, similarity=True)
                self._update_meter_set(average_meter_set, metrics)
                self._update_dataloader_metrics(
                    tqdm_dataloader, average_meter_set)

            average_metrics = average_meter_set.averages()
            with open(os.path.join(self.export_root, 'logs', f'test_metrics_{int(time.time())}.json'), 'w') as f:
                json.dump(average_metrics, f, indent=4)
        
        return average_metrics

    def bb_model_test(self):
        self.bb_model.eval()
        average_meter_set = AverageMeterSet()
        with torch.no_grad():
            tqdm_dataloader = tqdm(self.test_loader)
            for batch_idx, batch in enumerate(tqdm_dataloader):
                if isinstance(self.model, BERT) or isinstance(self.model, SASRec):
                    seqs, candidates, labels = batch
                    seqs, candidates, labels = seqs.to(self.device), candidates.to(self.device), labels.to(self.device)
                    scores = self.bb_model(seqs)[:, -1, :]
                    metrics = recalls_and_ndcgs_for_ks(scores.gather(1, candidates), labels, self.metric_ks)
                elif isinstance(self.model, NARM):
                    seqs, lengths, candidates, labels = batch
                    seqs, candidates, labels = seqs.to(self.device), candidates.to(self.device), labels.to(self.device)
                    lengths = lengths.flatten()
                    scores = self.bb_model(seqs, lengths)
                    metrics = recalls_and_ndcgs_for_ks(scores.gather(1, candidates), labels, self.metric_ks)

                self._update_meter_set(average_meter_set, metrics)
                self._update_dataloader_metrics(
                    tqdm_dataloader, average_meter_set)

            average_metrics = average_meter_set.averages()
            with open(os.path.join(self.export_root, 'logs', f'test_metrics_{int(time.time())}.json'), 'w') as f:
                json.dump(average_metrics, f, indent=4)
        
        return average_metrics

    def pre2post_padding(self, seqs):
        processed = torch.zeros_like(seqs)
        lengths = (seqs > 0).sum(-1).squeeze()
        for i in range(seqs.size(0)):
            processed[i, :lengths[i]] = seqs[i, seqs.size(1)-lengths[i]:]
        return processed

    def post2pre_padding(self, seqs):
        processed = torch.zeros_like(seqs)
        lengths = (seqs > 0).sum(-1).squeeze()
        for i in range(seqs.size(0)):
            processed[i, seqs.size(1)-lengths[i]:] = seqs[i, :lengths[i]]
        return processed

    def clip_gradients(self, limit=5):
        for p in self.model.parameters():
            nn.utils.clip_grad_norm_(p, 5)

    def _update_meter_set(self, meter_set, metrics):
        for k, v in metrics.items():
            meter_set.update(k, v)

    def _update_dataloader_metrics(self, tqdm_dataloader, meter_set):
        description_metrics = ['NDCG@%d' % k for k in self.metric_ks[:3]
                               ] + ['Recall@%d' % k for k in self.metric_ks[:3]]
        description = 'Eval: ' + \
            ', '.join(s + ' {:.3f}' for s in description_metrics)
        description = description.replace('NDCG', 'N').replace('Recall', 'R')
        description = description.format(
            *(meter_set[k].avg for k in description_metrics))
        tqdm_dataloader.set_description(description)

    def _create_optimizer(self):
        args = self.args
        param_optimizer = list(self.model.named_parameters())
        no_decay = ['bias', 'layer_norm']
        optimizer_grouped_parameters = [
            {
                'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
                'weight_decay': args.weight_decay,
            },
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0},
        ]
        if args.optimizer.lower() == 'adamw':
            return optim.AdamW(optimizer_grouped_parameters, lr=args.lr, eps=args.adam_epsilon)
        elif args.optimizer.lower() == 'adam':
            return optim.Adam(optimizer_grouped_parameters, lr=args.lr, weight_decay=args.weight_decay)
        elif args.optimizer.lower() == 'sgd':
            return optim.SGD(optimizer_grouped_parameters, lr=args.lr, weight_decay=args.weight_decay, momentum=args.momentum)
        else:
            raise ValueError

    def get_linear_schedule_with_warmup(self, optimizer, num_warmup_steps, num_training_steps, last_epoch=-1):
        # based on hugging face get_linear_schedule_with_warmup
        def lr_lambda(current_step: int):
            if current_step < num_warmup_steps:
                return float(current_step) / float(max(1, num_warmup_steps))
            return max(
                0.0, float(num_training_steps - current_step) / float(max(1, num_training_steps - num_warmup_steps))
            )

        return LambdaLR(optimizer, lr_lambda, last_epoch)

    def _create_loggers(self):
        root = Path(self.export_root)
        writer = SummaryWriter(root.joinpath('logs'))
        model_checkpoint = root.joinpath('models')

        train_loggers = [
            MetricGraphPrinter(writer, key='epoch',
                               graph_name='Epoch', group_name='Train'),
            MetricGraphPrinter(writer, key='loss',
                               graph_name='Loss', group_name='Train'),
        ]

        val_loggers = []
        for k in self.metric_ks:
            val_loggers.append(
                MetricGraphPrinter(writer, key='NDCG@%d' % k, graph_name='NDCG@%d' % k, group_name='Validation'))
            val_loggers.append(
                MetricGraphPrinter(writer, key='Recall@%d' % k, graph_name='Recall@%d' % k, group_name='Validation'))
        val_loggers.append(RecentModelLogger(model_checkpoint, f'checkpoint-recent{self.args.id}.pth'))
        val_loggers.append(BestModelLogger(
            model_checkpoint, metric_key=self.best_metric, filename=f'best_acc{self.args.id}_model.pth'))
        return writer, train_loggers, val_loggers

    def _create_state_dict(self):
        return {
            STATE_DICT_KEY: self.model.module.state_dict() if self.is_parallel else self.model.state_dict(),
            OPTIMIZER_STATE_DICT_KEY: self.optimizer.state_dict(),
        }

    def _needs_to_log(self, accum_iter):
        return accum_iter % self.log_period_as_iter < self.args.train_batch_size and accum_iter != 0
