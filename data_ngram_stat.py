import os
import subprocess
import time
import pickle
from collections import Counter
import math
import torch
import matplotlib.pyplot as plt


def load_data(arch, dataset_name, sampler, seq_num=5000):
    distill_dir = "/data/zhaoshilong/REA_with_llm"

    root = f'{distill_dir}/gen_data/{dataset_name}/{arch}_{seq_num}_100'

    for i in range(5):
        path = f'{sampler}{i}_dataset.pkl'
        try:
            with open(os.path.join(root, path), 'rb') as f:
                dataset = pickle.load(f)
            break
        except:
            continue
    seqs = dataset['seqs']
    return seqs


def extract_ngrams(sequences, n):
    ngrams = []
    for seq in sequences:  # Convert to list if tensor
        for i in range(len(seq) - n + 1):
            ngram = tuple(seq[i:i + n])
            ngrams.append(ngram)
    return ngrams

def compute_kl_divergence(p_dist, q_dist, eps=1e-10):
    kl_div = 0.0
    all_ngrams = set(p_dist.keys()).union(set(q_dist.keys()))
    
    for ngram in all_ngrams:
        p_prob = p_dist.get(ngram, eps)
        q_prob = q_dist.get(ngram, eps)
        kl_div += p_prob * math.log(p_prob / q_prob)
    
    return kl_div

def ngram_distribution(ngrams):
    total_count = len(ngrams)
    ngram_count = Counter(ngrams)
    return {ngram: count / total_count for ngram, count in ngram_count.items()}

def stat_ngram(arch, dataset_name, samplers, n, seq_num=5000):
    print(f'arch={arch}\tdataset_name={dataset_name}\tn={n}')
    seqs1 = load_data(arch, dataset_name, samplers[0])
    ngrams1 = extract_ngrams(seqs1, n)
    p_dist = ngram_distribution(ngrams1)

    for sampler in samplers[1:]:
        seqs2 = load_data(arch, dataset_name, sampler, seq_num)
        ngrams2 = extract_ngrams(seqs2, n)
        q_dist = ngram_distribution(ngrams2)
        kl_divergence = compute_kl_divergence(p_dist, q_dist)
        print(f'p:{samplers[0]}\t q:{sampler}\t\t kl:{kl_divergence}')

def plot_1gram_distribution(arch, dataset_name, sampler, seq_num=5000, color='blue', ylim=None):
    sequences = load_data(arch, dataset_name, sampler, seq_num)
    # Extract 1-grams (items)
    one_grams = [item for seq in sequences for item in seq]
    
    # Compute the distribution
    one_gram_count = Counter(one_grams)
    
    # Prepare data for plotting
    ids = list(one_gram_count.keys())
    counts = list(one_gram_count.values())
    
    sorted_ids, sorted_counts = zip(*sorted(zip(ids, counts)))

    # Create scatter plot
    plt.clf()
    plt.scatter(sorted_ids, sorted_counts, s=1, c=color)
    plt.xlabel('1-gram (item) ID')
    plt.ylabel('Count')
    plt.title(f'{arch}-{dataset_name}-{sampler} 1-gram Distribution')
    if ylim is not None:
        plt.ylim(ylim)
    plt.grid(True)
    plt.savefig(f'pics/{arch}-{dataset_name}-{sampler}-{seq_num}_1-gram_distribution.png')


samplers = ['self', 'random', 'autoregressive', 'llm_seq']
dataset_names = ['ml-1m', 'steam', 'beauty']
archs = ['narm', 'sas', 'bert']

# for i in range(1,4):
#     for dataset_name in dataset_names:
#         stat_ngram('bert', dataset_name, samplers, i)

stat_ngram(archs[0], dataset_names[0], ['self', 'autoregressive'], 1, 5001)
# stat_ngram(archs[0], dataset_names[1], ['self', 'llm_seq'], 2, 5000)
stat_ngram(archs[0], dataset_names[0], ['self', 'llm_seq'], 1, 5001)

# print(compute_kl_divergence({'a':1}, {'b':1}))
# print(compute_kl_divergence({'a':1}, {'a':0.7, 'b':0.3}))

# ylim=(0,100)
# plot_1gram_distribution(archs[0], dataset_names[1], samplers[0], 5000, ylim=ylim)
# plot_1gram_distribution(archs[0], dataset_names[1], samplers[2], 5000, ylim=ylim)
# plot_1gram_distribution(archs[0], dataset_names[1], samplers[2], 5001, color='red', ylim=ylim)
# plot_1gram_distribution(archs[0], dataset_names[1], samplers[3], 5000, ylim=ylim)
# plot_1gram_distribution(archs[0], dataset_names[1], samplers[3], 5010, color='red', ylim=ylim)