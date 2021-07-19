# Introduction

The RecSys-Model-Extraction-Attack repository is the PyTorch Implementation of Black-Box Attacks on Sequential Recommenders via Data-Free Model Extraction


## Citing 

Please cite the following paper if you use our methods in your research:
```
@inproceedings{yue2021black,
  title={Black-Box Attacks on Sequential Recommenders via Data-Free Model Extraction},
  author={Yue, Zhenrui and He, Zhankui and Zeng, Huimin and McAuley, Julian},
  booktitle={Proceedings of the 15th ACM Conference on Recommender Systems},
  year={2021}
}
```


## Requirements

PyTorch, pandas, wget, libarchive-c, faiss-cpu, tqdm, tensorboard. For our running environment see requirements.txt


## Train Black-Box Recommender Models

```bash
python train.py
```
Excecute the above command (with arguments) to train a black-box model, select datasets from Movielens 1M/20M, Beauty, Games, Steam and Yoochoose. Availabel models are NARM, SASRec and BERT4Rec. Trained black-box recommenders could be found under ./experiments/model-code/dataset-code/models/best_acc_model.pth


## Extract trained Black-Box Recommender Models

```bash
python distill.py
```
Excecute the above command (with arguments) to extract a white-box model, white-box model can also be chosen from NARM, SASRec and BERT4Rec. Trained models could be found under ./experiments/distillation_rank/distillation-specification/dataset-code/models/best_acc_model.pth


## Attack trained Black-Box Recommender Models

```bash
python attack.py
```
Run the above command (with arguments) to perform profile pollution attacks, logs will be save under ./experiments/attack_rank/distillation-specification/dataset-code/attack_bb_metrics.json


## Poison trained Black-Box Recommender Models

```bash
python retrain.py
```
Run the above command (with arguments) to perform data poisoning attacks, retrained model and logs will be save under ./experiments/retrained/distillation-specification/dataset-code/


## Performance

Our models are trained 100 / 20 epochs repspectively for appliances from REDD and UK-DALE dataset, all other parameters could be found in 'train.py' and 'utils.py'

### Black-Box and Extracted Models

<img src=pics/extraction.png width=1000>

### Profile Pollution Performance

<img src=pics/pollution.png width=1000>

### Data Poisoning Performance

<img src=pics/poisoning.png width=1000>


## Acknowledgement

During the implementation we base our code mostly on [Transformers](https://github.com/huggingface/transformers) from Hugging Face and [BERT4Rec](https://github.com/jaywonchung/BERT4Rec-VAE-Pytorch) by Jaewon Chung. Many thanks to these authors for their great work!
