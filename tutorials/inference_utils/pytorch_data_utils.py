import pandas as pd
import pickle as pkl
pd.options.mode.chained_assignment = None
import numpy as np
from typing import List, TypeVar
from rdkit import Chem, RDLogger

import torch
from torch.utils.data import Dataset, DataLoader, SequentialSampler

from transformers import DataCollatorWithPadding
from sklearn.metrics.pairwise import cosine_similarity 

RDLogger.DisableLog('rdApp.*')
PandasDataFrame = TypeVar('pandas.core.frame.DataFrame')
PyTorchDataLoader = TypeVar('torch.utils.data.DataLoader')


class PreProcessDataForInference():
    '''
    PreProcessDataForInference contains functions to:
    - generate one hot encoding vectors to represent endpoints and effects such that the main model class can make accurate predictions
    - Canonicalize SMILES to the RDKit format used during both pre-training and fine-tuning 

    If the data consists only of a single endpoint or effect no one hot encoding will be generated.
    '''
    def __init__(self, dataframe: PandasDataFrame):
        self.dataframe = dataframe

    def GetOneHotEnc(self, list_of_endpoints: List[str], list_of_effects: List[str]):
        '''
        Builds a one hot encoding vector in the exact same way as how the model was trained.

        Specify the endpoints and effects for which the model should generate its predictions.
        '''
        self.dataframe = self.__GetOneHotEndpoint(list_of_endpoints=list_of_endpoints)
        self.dataframe = self.__GetOneHotEffect(list_of_effects=list_of_effects)

        try:
            columns = self.dataframe.filter(like='OneHotEnc').columns.tolist()
            temp1 = np.array([el.tolist() for el in self.dataframe[columns[0]].values])
            for idx, col in enumerate(columns):
                try:
                    temp2 = np.array([el.tolist() for el in self.dataframe[columns[idx+1]].values])
                    temp1 = np.concatenate((temp1,temp2), axis=1)
                except:
                    pass
            self.dataframe['OneHotEnc_concatenated'] = temp1.tolist()
        except:
            self.dataframe['OneHotEnc_concatenated'] = np.zeros((len(self.dataframe), 1)).tolist()
            print('''Will use input 0 to network due to no Onehotencodings being present.''')

        return self.dataframe

    
    def GetCanonicalSMILES(self):
        '''
        Retrieves Canonical SMILES using RDKit.
        '''
        self.dataframe['SMILES_Canonical_RDKit'] = self.dataframe.SMILES.apply(lambda x: self.__CanonicalizeRDKit(x))

        return self.dataframe

    
    def __GetOneHotEndpoint(self, list_of_endpoints: List[str]):
        '''
        Builds one hot encoded numpy arrays for given endpoints. Groups EC10 and NOEC measurements by renaming NOEC --> EC10.
        '''
        if 'EC10' in list_of_endpoints:
            print(f"Renamed NOEC *EC10* in {sum(self.dataframe['endpoint'] == 'NOEC')} positions")
            self.dataframe.loc[self.dataframe.endpoint == 'NOEC', 'endpoint'] = 'EC10'
            try:
                list_of_endpoints.remove('NOEC')
            except:
                pass
    
        if len(list_of_endpoints) > 1:
            encoding_order = ['EC50', 'EC10']
            hot_enc_dict = dict(zip(encoding_order, np.eye(len(encoding_order), dtype=int).tolist()))
            self.dataframe = self.dataframe.reset_index().drop(columns='index', axis=1)
            try:
                encoded_clas = self.dataframe.endpoint.apply(lambda x: np.array(hot_enc_dict[x]))
                self.dataframe['OneHotEnc_endpoint'] = encoded_clas
            except:
                raise Exception('An unexpected error occurred.')

        else:
            print('''Did not return onehotencoding for Endpoint. Why? You specified only one Endpoint or you specified NOEC and EC10 which are coded to be the same endpoint.''')

        return self.dataframe

    def __GetOneHotEffect(self, list_of_effects: List[str]):
        '''
        Builds one hot encoded numpy arrays for given effects.
        '''
        if len(list_of_effects) > 1:
            hot_enc_dict = dict(zip(list_of_effects, np.eye(len(list_of_effects), dtype=int).tolist()))
            self.dataframe = self.dataframe.reset_index().drop(columns='index', axis=1)
            try:
                encoded_clas = self.dataframe.effect.apply(lambda x: np.array(hot_enc_dict[x]))
                self.dataframe['OneHotEnc_effect'] = encoded_clas
            except:
                raise Exception('An unexpected error occurred.')

        else:
            print('''Did not return onehotencoding for Effect. Why? You specified only one Effect.''')

        return self.dataframe

    ## Convenience functions
    def __CanonicalizeRDKit(self, smiles):
        try:
            return Chem.MolToSmiles(Chem.MolFromSmiles(smiles), canonical=True)
        except:
            return smiles


class Inference_dataset(Dataset):
    '''
    Class for efficient loading of data for inference.

    *Variables should be a list with column names corresponding to the column containing
    1. SMILES
    2. exposure_duration
    3. onehotencoding

    In the specified order.
    '''
    def __init__(self, df: PandasDataFrame, variables: List[str], tokenizer, max_len: int=100):
        self.df = df
        self.tokenizer = tokenizer
        self.variables = variables
        self.max_len = max_len

    def __len__(self):
        return len(self.df)

    def __getitem__(self, idx):
        row = self.df.iloc[idx]

        encodings = self.tokenizer.encode_plus(row[self.variables[0]], truncation=True, max_length=self.max_len)
        ids = torch.tensor(encodings['input_ids'])
        mask = torch.tensor(encodings['attention_mask'])
        dur = torch.tensor(row[self.variables[1]], dtype=torch.float32)
        onehot = torch.tensor(row[self.variables[2]], dtype=torch.float32)
        sample = {'input_ids': ids, 'attention_mask': mask, 'duration': dur, 'onehotenc': onehot}

        return sample

class BuildInferenceDataLoaderAndDataset:
    '''
    Class to build PyTorch Dataloader and Dataset for batch inference. 
    The Dataloader uses a SequentialSampler

    *Variables should be a list with column names corresponding to the column containing
    1. SMILES
    2. exposure_duration
    3. onehotencoding

    In the specified order.
    '''
    def __init__(self, df: PandasDataFrame, variables: List[str], tokenizer, batch_size: int=8, max_length: int=100, seed: int=42, num_workers: int=0):
        self.df = df
        self.bs = batch_size
        self.tokenizer = tokenizer
        self.max_len = max_length
        self.variables = variables
        self.seed = seed
        self.collator = DataCollatorWithPadding(tokenizer=self.tokenizer, padding='longest', return_tensors='pt')
        self.num_workers = num_workers
        
        self.dataset = Inference_dataset(df, self.variables, self.tokenizer, max_len=self.max_len)

        sampler = SequentialSampler(self.dataset)
        self.dataloader = DataLoader(self.dataset, sampler=sampler, batch_size=self.bs, collate_fn=self.collator, num_workers=self.num_workers)

def __loadtrainingdf__(MODELTYPE, PREDICTION_SPECIES, PREDICTION_ENDPOINT):
    df = pd.read_pickle('../data/development/Preprocessed_complete_data.pkl.zip', compression='zip')
    if MODELTYPE == 'EC50EC10':
        PREDICTION_ENDPOINT = 'EC50,EC10'
    # Get training set
    df = df[(df.species_group == PREDICTION_SPECIES) & (df.endpoint.isin(PREDICTION_ENDPOINT.split(',')))]
    df = df.drop_duplicates(subset=['SMILES_Canonical_RDKit','species_group','endpoint','effect'])    
    df = PreProcessDataForInference(df).GetCanonicalSMILES()
    return df

def __loadpredictionsdf__():
    df = pd.read_pickle(f'../data/tutorials/predictions/combined_predictions_and_errors.pkl.zip', compression='zip')
    df = df.loc[:,~df.columns.duplicated()].copy()
    return df

def __loadCLSembeddings__(MODELTYPE, PREDICTION_SPECIES):
    df = pd.read_pickle(f'../data/tutorials/predictions/{MODELTYPE}_{PREDICTION_SPECIES}_CLS_embeddings.pkl.zip', compression='zip')
    return df

def check_training_data_from_scratch(df, model_type, species_group, endpoint, effect, path_to_data=None):
    """
    Function to check whether chemical is present in our training set (takes much longer to run compared to next function)
    """
    endpoint_matches = []
    effect_matches = []
    
    training_data = __loadtrainingdf__(model_type, species_group, endpoint)

    for SMILES in df.SMILES_Canonical_RDKit.drop_duplicates().tolist():
        data = training_data[training_data.SMILES_Canonical_RDKit==SMILES]
        if data.empty:
            endpoint_match=0
            effect_match=0
        else:
            data = data[data.endpoint == endpoint]
            endpoint_match = 1 if SMILES in data.SMILES_Canonical_RDKit.tolist() else 0
            data = data[data.effect == effect]
            effect_match = 1 if SMILES in data.SMILES_Canonical_RDKit.tolist() else 0

        endpoint_matches.append(endpoint_match)
        effect_matches.append(effect_match)

    df['endpoint match'] = endpoint_matches
    df['effect match'] = effect_matches
    
    return df

def check_training_data(df, model_type, species_group, endpoint, effect):

    endpoint_matches = []
    effect_matches = []
    
    all_preds = __loadpredictionsdf__()
    all_preds = all_preds[['SMILES_Canonical_RDKit',f'{model_type}_{species_group}_{endpoint}_{effect} endpoint match', f'{model_type}_{species_group}_{endpoint}_{effect} effect match']]
 
    for SMILES in df.SMILES_Canonical_RDKit.tolist():
        data = all_preds[all_preds.SMILES_Canonical_RDKit==SMILES]
        if data.empty:
            endpoint_match=0
            effect_match=0
        else:
            endpoint_match = data[f'{model_type}_{species_group}_{endpoint}_{effect} endpoint match'].iloc[0]
            effect_match = data[f'{model_type}_{species_group}_{endpoint}_{effect} effect match'].iloc[0]

        endpoint_matches.append(endpoint_match)
        effect_matches.append(effect_match)

    df['endpoint match'] = endpoint_matches
    df['effect match'] = effect_matches
    
    return df

def check_closest_chemical(results, MODELTYPE, PREDICTION_SPECIES, PREDICTION_ENDPOINT, PREDICTION_EFFECT):
    """
    Checks the closest chemical by means of cosine similarity of CLS-embedding inside our training set.
    """
    training_data = __loadtrainingdf__(MODELTYPE, PREDICTION_SPECIES, PREDICTION_ENDPOINT)
    training_data = training_data.drop_duplicates(subset=['SMILES_Canonical_RDKit'])

    cls_df = __loadCLSembeddings__(MODELTYPE, PREDICTION_SPECIES)
    training_data = training_data.merge(cls_df, on='SMILES_Canonical_RDKit')
    cls_training_data = np.asarray(training_data.CLS_embeddings.tolist(), dtype=np.float32)
    cls = np.asarray(results.CLS_embeddings.tolist(), dtype=np.float32)
    if len(results) ==1:
        cls = cls.reshape(1,-1)

    cossim = cosine_similarity(cls_training_data, cls)
    idx_closest = np.argmax(cossim, axis=0)
    similarity_score_closest = np.diag(cossim[idx_closest])
    results['most similar chemical'] = training_data.SMILES_Canonical_RDKit.iloc[idx_closest].tolist()
    results['max cosine similarity'] = similarity_score_closest
    results['mean cosine similarity'] = np.mean(cossim, axis=0)

    return results 

def check_valid_smiles(smiles):
    mol = Chem.MolFromSmiles(smiles, sanitize=False)
    if (mol is not None) and ('*' not in smiles):
        # Syntactically valid
        return None
    else:
        return 'SMILES not valid'

def check_valid_chemistry(smiles): 
    if '*' in smiles:
        return 'Not chemically valid'
    try:
        mol = Chem.MolFromSmiles(smiles, sanitize=False)  
        Chem.SanitizeMol(mol)
        # Chemically valid
        return None
    except:
        # Chemically invalid
        return 'Not chemically valid'