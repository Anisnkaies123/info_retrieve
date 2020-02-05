import tensorflow as tf
import os
import pickle
import datetime
import pandas as pd
import numpy as np
import logging
import random
import sys
sys.path.append('/polyaxon-data/goldenretriever')

from sklearn.metrics.pairwise import cosine_similarity
from sklearn.model_selection import train_test_split
from scipy.stats.mstats import rankdata
from absl import flags, app
from polyaxon_client.tracking import Experiment, get_log_level

from src.model import GoldenRetriever, GoldenRetriever_BERT, GoldenRetriever_ALBERT
from src.dataloader import kb_train_test_split
from src.eval_model import mrr, recall_at_n, get_eval_dict
from src.kb_handler import kb, kb_handler


FLAGS = flags.FLAGS

# For logging
def setup_logging():
    log_level = get_log_level()
    if log_level is None:
        log_level = logging.INFO
    logging.basicConfig(level=log_level)


setup_logging()
logger = logging.getLogger(__name__)
logger.info("Starting experiment")
# experiment = Experiment()


# eg. 'albert' or 'bert' or 'USE'
flags.DEFINE_string(
    "model_name", None,
    "The name of the model that needs to be finetuned and evaluated"
)

# eg. 42
flags.DEFINE_integer(
    "random_seed", None,
    "The selected random seed to split train-validation dataset"
)

# eg. 16
flags.DEFINE_integer(
    "train_batch_size", None,
    "The selected random seed to split train-validation dataset"
)

# eg. 16
flags.DEFINE_integer(
    "predict_batch_size", None,
    "The selected random seed to split train-validation dataset"
)

# eg. 5e-5
flags.DEFINE_float(
    "learning_rate", None,
    "The initial learning rate for adam optimizer"
)

# eg. 0.9
flags.DEFINE_float(
    "beta_1", None,
    "beta_1 for adam optimizer"
)

# eg. 0.999
flags.DEFINE_float(
    "beta_2", None,
    "beta_2 for adam optimizer"
)

# eg. 1e-07
flags.DEFINE_float(
    "epsilon", None,
    "epsilon for adam optimizer"
)

# eg. 30
flags.DEFINE_integer(
    "num_epochs", None,
    "The number of epochs for training"
)

# eg. 128 or 256 or 512
flags.DEFINE_integer(
    "max_seq_length", None,
    "The maximum total input sequence length after tokenization. "
    "Sequences longer than this will be truncated, and sequences shorter "
    "than this will be padded."
)

# eg. 'cosine', 'contrastive', 'triplet'
flags.DEFINE_string(
    "loss_type", None,
    "The type of training loss to be used for optimization"
)

# eg. 0.3
flags.DEFINE_float(
    "margin", None,
    "The margin value to be used if loss_type == contrastive or triplet"
)

# eg. train_eval or eval_only
flags.DEFINE_string(
    "task_type", None,
    "To define whether to train or evaluate or both train and evaluate the chosen model"
)

# eg. '/polyaxon-data/model/USE/best/1'
flags.DEFINE_string(
    "eval_model_dir", None,
    "To define whether to train or evaluate or both train and evaluate the chosen model"
)

# eg. 5
flags.DEFINE_integer(
    "early_stopping_steps", None,
    "How many epochs without improvement in loss for early stopping"
)


def _flags_to_file(flag_objs, file_path):
    with open(file_path, 'w+') as f:
        for flag in flag_objs:
            f.write("--" + flag.name + "=" + str(flag.value) + "\n")


def _generate_neg_ans(df, train_dict):
    
    np.random.seed(FLAGS.random_seed)

    for kb, ans_pos_idxs in train_dict.items():
        keys = []
        shuffled_ans_pos_idxs = ans_pos_idxs.copy()
        random.shuffle(shuffled_ans_pos_idxs)
        ans_neg_idxs = shuffled_ans_pos_idxs
        
        for i in range(len(ans_neg_idxs)):
            v=0
            while ans_pos_idxs[i] == ans_neg_idxs[i]:
                ans_neg_idxs[i] = shuffled_ans_pos_idxs[v]
                v += 1


        keys.append(ans_pos_idxs)
        keys.append(np.array(ans_neg_idxs))

        train_dict[kb] = keys 
    
    return train_dict


def _create_dataset(data_type, batch_size, query=[], response=[], neg_response=[]):
    if data_type == 'train':
        def gen():
            for ex in zip(query, response, neg_response):
                yield(ex[0], ex[1], ex[2])

        dataset = tf.data.Dataset.from_generator( 
            gen, 
            (tf.string, tf.string, tf.string), 
            (tf.TensorShape([]), tf.TensorShape([]), tf.TensorShape([]))
            )

        return dataset.shuffle(128).batch(batch_size)

    else:
        def gen():
            for ex in zip (query, response):
                yield(ex[0], ex[1])
        
        dataset = tf.data.Dataset.from_generator( 
            gen, 
            (tf.string, tf.string), 
            (tf.TensorShape([]), tf.TensorShape([]))
            )
        
        return dataset.shuffle(128).batch(batch_size)


def _convert_bytes_to_string(byte):
    return str(byte, 'utf-8')


def gen(batch_size, query, response, neg_response):
    num_samples = len(query)
    
    for offset in range(0, num_samples, batch_size):
        q_batch = query[offset:offset+batch_size]
        r_batch = response[offset:offset+batch_size]
        neg_r_batch = response[offset:offset+batch_size]
    
        yield(q_batch, r_batch, neg_r_batch)


def main(_):
    tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.INFO)

    # Define file/directory paths
    MAIN_DIR = '/polyaxon-data/goldenretriever'
    MODEL_DIR = os.path.join(MAIN_DIR, 'model_test', FLAGS.model_name)
    MODEL_BEST_DIR = os.path.join(MAIN_DIR, 'model_test', FLAGS.model_name, 'best')
    MODEL_LAST_DIR = os.path.join(MAIN_DIR, 'model_test', FLAGS.model_name, 'last')
    EVAL_SCORE_DIR = os.path.join(MAIN_DIR, 'results', FLAGS.model_name + '_eval_scores.xlsx' )
    EVAL_DICT_DIR = os.path.join(MAIN_DIR, 'results', FLAGS.model_name + '_eval_details.pickle')

    logger.info(f"Models will be saved at: {MODEL_DIR}")
    logger.info(f"Best model will be saved at: {MODEL_BEST_DIR}")
    logger.info(f"Last trained model will be saved at {MODEL_LAST_DIR}")
    logger.info(f"Saving Eval_Score at: {EVAL_SCORE_DIR}")
    logger.info(f"Saving Eval_Dict at: {EVAL_DICT_DIR}")

    # Create training set based on chosen random seed
    logger.info("Generating training set")

    # Get df using kb_handler
    kbh = kb_handler()
    kbs = kbh.load_sql_kb(cnxn_path='/polyaxon-data/goldenretriever/db_cnxn_str.txt', kb_names=['nrf', 'PDPA'])

    train_dict = dict()
    test_dict = dict()
    df_list = []

    for single_kb in kbs:
        kb_df = single_kb.create_df()
        df_list.append(kb_df)
        idx = np.array(kb_df.index.tolist())
        train_idx, test_idx = train_test_split(idx, test_size=0.2, random_state=FLAGS.random_seed)
        
        train_dict[single_kb.name] = train_idx
        test_dict[single_kb.name] = test_idx
    
    df = pd.concat(df_list)

    train_dict_with_neg = _generate_neg_ans(df, train_dict)
    train_pos_idxs = np.concatenate([v[0] for k,v in train_dict_with_neg.items()], axis=0)
    train_neg_idxs = np.concatenate([v[1] for k,v in train_dict_with_neg.items()], axis=0)

    train_query = df.loc[train_pos_idxs].query_string.tolist()
    train_response = df.loc[train_pos_idxs].processed_string.tolist()
    train_neg_response = df.loc[train_neg_idxs].processed_string.tolist()
    
    train_dataset_loader = gen(FLAGS.train_batch_size, train_query, train_response, train_neg_response)

    # Create training tf dataset generator
    logger.info("Creating dataset")
    # train_dataset = _create_dataset('train', FLAGS.train_batch_size, query=train_query, response=train_response, neg_response=train_neg_response)

    # Instantiate chosen model
    logger.info(f"Instantiating model: {FLAGS.model_name}")
    models = {
        "albert": GoldenRetriever_ALBERT,
        "bert": GoldenRetriever_BERT,
        "USE": GoldenRetriever
    }

    if FLAGS.model_name not in models:
        raise ValueError("Model not found: %s" % (FLAGS.model_name))

    model = models[FLAGS.model_name]()

    # Set optimizer parameters
    model.opt_params = {'learning_rate': FLAGS.learning_rate,'beta_1': FLAGS.beta_1,'beta_2': FLAGS.beta_2,'epsilon': FLAGS.epsilon}

    # Fine-tune
    logger.info("Fine-tuning model")
    if FLAGS.task_type == 'train_eval':
            # Required for contrastive loss
            # label = tf.placeholder(tf.int32, [None], name='label')

            earlystopping_counter = 0
            
            for i in range(FLAGS.num_epochs):
                epoch_start_time = datetime.datetime.now()
                logger.info(f"Running Epoch #: {i}")

                cost_mean_total = 0
        
                batch_counter = 0
                for q, r, neg_r in train_dataset_loader:

                    if batch_counter == 0:
                        logger.info(f"Training batches of size: {len(r)}")
        
                    cost_mean_batch = model.finetune(question=q, answer=r, context=r, \
                                                     neg_answer=neg_r, neg_answer_context=neg_r, \
                                                     margin=FLAGS.margin, loss=FLAGS.loss_type)
    
                    cost_mean_total += cost_mean_batch
                    batch_counter += 1

                logger.info(f'Number of batches trained: {batch_counter}')
                logger.info(f'Loss for Epoch #{i}: {cost_mean_total}')

                # Get model for first epoch
                if i == 0:
                    lowest_cost = cost_mean_total
                    best_epoch = i
                    earlystopping_counter = 0
                    os.makedirs(os.path.join(MODEL_BEST_DIR, str(i)))
                    model.export(os.path.join(MODEL_BEST_DIR, str(i)))
                    _flags_to_file(FLAGS.get_key_flags_for_module(sys.argv[0]),
                                  os.path.join(MODEL_BEST_DIR, str(i), 'train.cfg'))

                # Model checkpoint
                if cost_mean_total < lowest_cost:
                    best_epoch = i
                    lowest_cost = cost_mean_total
                    os.makedirs(os.path.join(MODEL_BEST_DIR, str(i)))
                    model.export(os.path.join(MODEL_BEST_DIR, str(i)))
                    _flags_to_file(FLAGS.get_key_flags_for_module(sys.argv[0]),
                                  os.path.join(MODEL_BEST_DIR, str(i), 'train.cfg'))
                    logger.info(f"Saved best model with cost of {lowest_cost} for Epoch #{i}")
                else:
                    # Activate early stopping counter
                    earlystopping_counter += 1

                # experiment.log_metrics(steps=i, loss=cost_mean_total)

                # Early stopping
                if earlystopping_counter == FLAGS.early_stopping_steps:
                    logger.info("Early stop executed")
                    model.export(MODEL_LAST_DIR)
                    _flags_to_file(FLAGS.get_key_flags_for_module(sys.argv[0]),
                                  os.path.join(MODEL_LAST_DIR, 'train.cfg'))
                    break
                
                epoch_end_time = datetime.datetime.now()
                logger.info(f"Time Taken for Epoch #{i}: {epoch_end_time - epoch_start_time}")
                logger.info(f"Average time Taken for each batch: {(epoch_end_time - epoch_start_time)/batch_counter}")

    # Restore best model. User will have to define path to model if only eval is done.
    if FLAGS.task_type == 'train_eval':
        logger.info("Restoring model")
        model.restore(os.path.join(MODEL_BEST_DIR, str(best_epoch)))
    else:
        model.restore(FLAGS.eval_model_dir)

    # Output scores based on eval_script
    logger.info("Evaluating model")
    eval_start_time = datetime.datetime.now()

    eval_dict = {}

    for kb_name in df.kb_name.unique():

        logger.info(f"\n {datetime.datetime.now()} - Evaluating on {kb_name} \n")

        # dict stores eval metrics and relevance ranks
        eval_kb_dict = {}

        # test-mask is a int array
        # that chooses specific test questions
        # e.g.  test_mask [True, True, False]
        #       query_idx = [0,1]
        kb_df = df.loc[df.kb_name == kb_name]
        kb_idx = df.loc[df.kb_name == kb_name].index
        test_mask = np.isin(kb_idx, test_dict[kb_name])
        # test_idx_mask = np.arange(len(kb_df))[test_mask]

        # get string queries and responses, unduplicated as a list
        kb_df = kb_df.reset_index(drop=True)
        query_list = kb_df.query_string.tolist()
        response_list_w_duplicates = kb_df.processed_string.tolist()
        response_list = kb_df.processed_string.drop_duplicates().tolist() 

        # this index list is important
        # it lists the index of the correct answer for every question
        # e.g. for 20 questions mapped to 5 repeated answers
        # it has 20 elements, each between 0 and 4
        response_idx_list = [response_list.index(nonunique_response_string) 
                            for nonunique_response_string in response_list_w_duplicates]
        response_idx_list = np.array(response_idx_list)[[test_mask]]
        
        encoded_queries = model.predict(query_list, type='query')
        encoded_responses = model.predict(response_list, type='response')

        # get matrix of shape [Q_test x Responses]
        # this holds the relevance rankings of the responses to each test ques
        test_similarities = cosine_similarity(encoded_queries[test_mask], encoded_responses)
        answer_ranks = test_similarities.shape[-1] - rankdata(test_similarities, axis=1) + 1

        # ranks_to_eval
        ranks_to_eval = [answer_rank[correct_answer_idx] 
                        for answer_rank, correct_answer_idx 
                        in zip( answer_ranks, response_idx_list )]


        # get eval metrics -> eval_kb_dict 
        # store in one large dict -> eval_dict
        eval_kb_dict = get_eval_dict(ranks_to_eval)
        eval_kb_dict['answer_ranks'] = answer_ranks
        eval_kb_dict['ranks_to_eval'] = ranks_to_eval
        eval_dict[kb_name] = eval_kb_dict.copy()

    # overall_eval is a dataframe that 
    # tracks performance across the different knowledge bases
    # but individually
    overall_eval = pd.DataFrame(eval_dict).T.drop(['answer_ranks', 'ranks_to_eval'], axis=1)

    # Finally we get eval metrics for across all different KBs
    correct_answer_ranks_across_kb = []
    for key in eval_dict.keys():
        correct_answer_ranks_across_kb.extend(eval_dict[key]['ranks_to_eval'])
        
    # get eval metrics across all knowledge bases combined
    across_kb_scores = get_eval_dict(correct_answer_ranks_across_kb)
    across_kb_scores_ = {'Across_all_kb':across_kb_scores}
    across_kb_scores_ = pd.DataFrame(across_kb_scores_).T

    overall_eval = pd.concat([overall_eval,across_kb_scores_])
    print(overall_eval)

    # save the scores and details for later evaluation. WARNING: User will need to create the necessary directories to save df
    overall_eval.to_excel(EVAL_SCORE_DIR)
    with open(EVAL_DICT_DIR, 'wb') as handle:
        pickle.dump(eval_dict, handle)

    eval_end_time = datetime.datetime.now()
    logger.info(f"Time Taken for Eval : {eval_end_time - eval_start_time}")

if __name__ == "__main__":
    app.run(main)


# python /polyaxon-data/goldenretriever/src/finetune_eval.py \
#     --model_name='USE' \
#     --random_seed=42 \
#     --train_batch_size=2 \
#     --predict_batch_size=2 \
#     --learning_rate=5e-5 \
#     --beta_1=0.9 \
#     --beta_2=0.999 \
#     --epsilon=1e-07 \
#     --num_epochs=1 \
#     --max_seq_length=256 \
#     --loss_type='triplet' \
#     --margin=0.3 \
#     --task_type='train_eval' \
#     --early_stopping_steps=5
