import pandas as pd
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

def question_cleaner(df_query):
    kb=([int(xx) for xx in (df_query[3].iloc[0]).split(' ')])
    gt = [int(xx) for xx in (df_query[2].iloc[0]).split(' ')]
    ct=0
    negg=0
    withans=[]
    for ii in range(len(df_query)):
        kb=[int(xx) for xx in (df_query[3].iloc[ii]).split(' ')]
        gt = [int(xx) for xx in (df_query[2].iloc[ii]).split(' ')]
        if bool(set(gt) & set(kb)):
            withans.append(ii)
        else:
            negg+=1
    print('total:{}, removed:{}, remainder:{}'.format(len(df_query), negg, len(withans)))
    return df_query.iloc[withans]

def display_qn_and_ans(df_query, df_doc, index=0):
    kb=[int(xx) for xx in (df_query[3].iloc[index]).split(' ')]
    gt = [int(xx) for xx in (df_query[2].iloc[index]).split(' ')]
    print('Question is: {}'.format(df_query['text'].iloc[index]))
    print('Answer index: ', gt)
    print('Answers: ', df_doc.loc[gt, 'text'].values)

def read_txt(path):
    """Used with split_txt() to read and split kb into clauses"""
    with open(path, 'r', encoding="utf-8") as f:
        text = f.readlines()
    return text

def split_txt(text, qa=False):
    """Splits a text document into clauses based on whitespaces. 
    Additionally, reads a faq document by assuming that the first line is a question 
    between each whitespaced group
    """
    condition_terms = []
    stringg=''
    for tex in text:
        if (tex=='\n'):
            if (stringg != ''):
                condition_terms.append(stringg)
                stringg=''
            else: pass
        else: stringg+=tex
    if qa:
        condition_context = [x.split('\n')[0] for x in condition_terms]
        condition_terms = [' '.join(x.split('\n')[1:]) for x in condition_terms]
    condition_terms=[x.replace('\n', '. ') for x in condition_terms] # not sure how newlines are tokenized
    condition_terms=[x.replace('.. ', '. ').rstrip() for x in condition_terms] # remove artifact
    if qa:
        return condition_terms, condition_context
    else: return condition_terms

def aiap_qna(question, answer_array, aiap_qa, model, k=1):
    similarity_score=cosine_similarity(answer_array, model.predict([question], type='query'))
    sortargs=np.flip(similarity_score.argsort(axis=0))
    sortargs=[x[0] for x in sortargs]
    sorted_ans=[]
    for indx in range(k):
        sorted_ans.append(aiap_qa[sortargs[indx]])
    return sorted_ans, sortargs, similarity_score

def aiap_qna_quickscore(aiap_context, answer_array, aiap_qa, model, k=1):
    """Quickly scores the model against the aiap qna dataset. 
    This function works because the order of questions and answers are synched in the list.
    """
    score=0
    for ii, qn in enumerate(aiap_context):
        _, sortargs, simscore = aiap_qna(qn, answer_array, aiap_qa, model, k)
        # print(qn, aiap_qa[sortargs[0]], simscore)
        if bool(set([ii]) & set(sortargs[:k])):
            score+=1
    return score/len(aiap_context)

def ranker(model, question_vectors, df_query, df_doc):
    """for model evaluation on InsuranceQA datset"""
    predictions=[]
    gts=[]
    for ii, question_vector in enumerate(question_vectors):
        kb=[int(xx) for xx in (df_query[3].iloc[ii]).split(' ')]
        gt = [int(xx) for xx in (df_query[2].iloc[ii]).split(' ')]
        doc_vectors = model.predict(df_doc.loc[kb]['text'].tolist())
        cossim = cosine_similarity(doc_vectors, question_vector.reshape(1, -1))
        sortargs=np.flip(cossim.argsort(axis=0))
        returnedans = [kb[jj[0]] for jj in sortargs]
        predictions.append(returnedans)
        gts.append(gt)
    return predictions, gts
        
def scorer(predictions, gts, k=3):
    """For model evaluation on InsuranceQA datset. Returns score@k."""
    score=0
    total=0
    for gt, prediction in zip(gts, predictions):
        if bool(set(gt) & set(prediction[:k])):
            score+=1
        total+=1
    return score/total

def make_pred(row, gr, query_col_name='queries'):
    """Make line by line predictions, returns top 3 index of kb."""
    txt, ind = gr.make_query(row['queries'], top_k=3, index=True)
    return ind

def make_iscorr(row, prediction_col_name='predictions', answer_col_name='answer'):
    """Calculates accuracy @3."""
    if bool(set(row[answer_col_name]) & set(row[prediction_col_name])):
        return 1
    else: return 0
    
def make_closewrong(row, prediction_col_name='predictions', answer_col_name='answer'):
    """Find index of wrong answer with highest similarity score aka hardmining."""
    try: return [x for x in row[prediction_col_name] if x not in row[answer_col_name]][0]
    except: return 1 #Just return the most common class as the negative eg.
    
def make_finetune(row, gr, query_col_name='queries', answer_col_name='answer', closewrong_col_name='closewrong'):
    """Stochastic finetuning sample by sample."""
    loss = gr.finetune([row[query_col_name]], [gr.text[row[answer_col_name][0]]], [gr.text[row[answer_col_name][0]]], [gr.text[row[closewrong_col_name]]], [gr.text[row[closewrong_col_name]]])
    print(loss)
    
def make_contrastive_finetune(row, gr, query_col_name='queries', answer_col_name='answer', closewrong_col_name='closewrong'):
    """Stochastic finetuning for contrastive loss."""
    loss = gr.finetune(question=[row[query_col_name]], answer=[gr.text[row[answer_col_name][0]]], context=[gr.text[row[answer_col_name][0]]], label=[1])
    print('1: ', loss)
    loss = gr.finetune(question=[row[query_col_name]], answer=[gr.text[row[closewrong_col_name]]], context=[gr.text[row[closewrong_col_name]]], label=[0])
    print('0: ', loss)