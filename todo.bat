@REM dataset 'ml-1m', 'ml-20m', 'steam', 'games', 'beauty', 'beauty_dense', 'yoochoose'
@REM model None, 'bert', 'sas', 'narm'

@REM conda activate cuda
@REM bert
@REM On Google Colab ing
@REM python train.py --dataset_code steam --model_code bert
@REM python train.py --dataset_code yoochoose --model_code bert

@REM sas
@REM Done!
@REM python train.py --dataset_code ml-1m --model_code sas
@REM 太慢了
@REM python train.py --dataset_code ml-20m --model_code sas
@REM 太慢了
@REM python train.py --dataset_code steam --model_code sas
@REM Done
@REM python train.py --dataset_code games --model_code sas
@REM On WK Running...
@REM python train.py --dataset_code beauty --model_code sas
@REM python train.py --dataset_code beauty_dense --model_code sas
@REM 太大了
@REM python train.py --dataset_code yoochoose --model_code sas

@REM narm
@REM 太大了
@REM python train.py --dataset_code ml-20m --model_code narm
@REM 太大了
@REM python train.py --dataset_code steam --model_code narm
@REM 太大了
@REM python train.py --dataset_code yoochoose --model_code narm


@REM distill
@REM running
python distill.py --dataset_code beauty --model_code bert --bb_model_code bert --num_generated_seqs 5000
python distill.py --dataset_code beauty_dense --model_code bert --bb_model_code bert --num_generated_seqs 5000
python distill.py --dataset_code games --model_code bert --bb_model_code bert --num_generated_seqs 5000
python distill.py --dataset_code ml-1m --model_code bert --bb_model_code bert --num_generated_seqs 5000

@REM On wk running
python distill.py --dataset_code beauty --model_code sas --bb_model_code bert --num_generated_seqs 5000