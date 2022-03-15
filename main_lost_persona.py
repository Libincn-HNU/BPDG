#!/usr/bin/env python3
# coding:utf-8

# Copyright (c) Tsinghua university conversational AI group (THU-coai).
# This source code is licensed under the MIT license.
import codecs

import torch
import random
from model.utils import load_openai_weights_chinese, set_seed
from model.transformer_model_s2s_lost import TransformerLostModel
from model.text import myVocab
from config import get_model_config_lost_persona, get_test_config_lost_persona
from collections import Counter
import json
import numpy as np
import warnings

warnings.filterwarnings("ignore")
class Model:
    """
    This is an example model. It reads predefined dictionary and predict a fixed distribution.
    For a correct evaluation, each team should implement 3 functions:
    next_word_probability
    gen_response
    """
    def __init__(self):
        """
        Init whatever you need here
        with codecs.open(vocab_file, 'r', 'utf-8') as f:
            vocab = [i.strip().split()[0] for i in f.readlines() if len(i.strip()) != 0]
        self.vocab = vocab
        self.freqs = dict(zip(self.vocab[::-1], range(len(self.vocab))))
        """
        # vocab_file = 'vocab.txt'
        model_config = get_model_config_lost_persona()
        test_config = get_test_config_lost_persona()
        set_seed(test_config.seed)
        device = torch.device(test_config.device)
        vocab = myVocab(model_config.vocab_path)
        self.vocab = vocab
        transformer = TransformerLostModel(n_layers=model_config.n_layers,
                                              n_embeddings=len(vocab),
                                              n_pos_embeddings=model_config.n_pos_embeddings,
                                              embeddings_size=model_config.embeddings_size,
                                              padding_idx=vocab.pad_id,
                                              n_heads=model_config.n_heads,
                                              dropout=model_config.dropout,
                                              embed_dropout=model_config.embed_dropout,
                                              attn_dropout=model_config.attn_dropout,
                                              ff_dropout=model_config.ff_dropout,
                                              bos_id=vocab.bos_id,
                                              eos_id=vocab.eos_id,
                                              max_seq_len=model_config.max_seq_len,
                                              beam_size=model_config.beam_size,
                                              length_penalty=model_config.length_penalty,
                                              n_segments=model_config.n_segments,
                                              annealing_topk=model_config.annealing_topk,
                                              temperature=model_config.temperature,
                                              annealing=model_config.annealing,
                                              diversity_coef=model_config.diversity_coef,
                                              diversity_groups=model_config.diversity_groups)
        transformer = transformer.to(device)
        state_dict = torch.load(test_config.last_checkpoint_path, map_location=device)
        temp = dict(state_dict['model'])
        keys = list(temp.keys())
        for key in keys:
            # new_key = '.'.join([i for i in key.split('.') if i != 'module'])
            new_key = key.replace('.module', '')
            temp[new_key] = temp.pop(key)
        transformer.load_state_dict(temp)
        transformer.eval()
        self.model_config = model_config
        self.test_config = test_config
        self.transformer = transformer
        self.device = device
        print('Weights loaded from {}'.format(test_config.last_checkpoint_path))

    def next_word_probability(self, context, partial_out):
        """
        Return probability distribution over next words given a partial true output.
        This is used to calculate the per-word perplexity.

        :param context: dict, contexts containing the dialogue history and personal
                        profile of each speaker
                        this dict contains following keys:

                        context['dialog']: a list of string, dialogue histories (tokens in each utterances
                                           are separated using spaces).
                        context['uid']: a list of int, indices to the profile of each speaker
                        context['profile']: a list of dict, personal profiles for each speaker
                        context['responder_profile']: dict, the personal profile of the responder

        :param partial_out: list, previous "true" words
        :return: a list, the first element is a dict, where each key is a word and each value is a probability
                         score for that word. Unset keys assume a probability of zero.
                         the second element is the probability for the EOS token

        e.g.
        context:
        { "dialog": [ ["How are you ?"], ["I am fine , thank you . And you ?"] ],
          "uid": [0, 1],
          "profile":[ { "loc":"Beijing", "gender":"male", "tag":"" },
                      { "loc":"Shanghai", "gender":"female", "tag":"" } ],
          "responder_profile":{ "loc":"Beijing", "gender":"male", "tag":"" }
        }

        partial_out:
        ['I', 'am']

        ==>  {'fine': 0.9}, 0.1
        """
        '''
        # freqs = copy.deepcopy(self.freqs)
        freqs = self.freqs
        for i in partial_out:
            if i in freqs:
                freqs[i] += 1000
        '''
        if 'responder_profile' in context:
            responder_profile = context['responder_profile']
            tag = responder_profile['tag'].replace(' ', '')
        else:
            responder_profile = context['response_profile']
            tag = ';'.join(responder_profile['tag']).replace(' ', '')
        dialog = context['dialog']
        # tag = ';'.join(responder_profile['tag']).replace(' ', '')
        loc = ';'.join(responder_profile['loc'].split()).replace(' ', '')
        gender = '男' if responder_profile['gender'] == 'male' else '女'
        persona = '性别:' + gender + ',' + '地点:' + loc + ',' + '标签:' + tag
        profile_ids = self.vocab.string2ids(' '.join(persona))
        dialog_ids = [self.vocab.string2ids(' '.join(i[0].replace(' ', ''))) for i in dialog]
        profile = [self.vocab.eos_id] + profile_ids + [self.vocab.eos_id]
        if len(dialog_ids):
            history_cat = [self.vocab.eos_id]
            for i in dialog_ids[:-1]:
                history_cat.extend(i)
                history_cat.extend([self.vocab.spl_id])
            history_cat.extend(dialog_ids[-1])
            history_cat.extend([self.vocab.eos_id])
        else:
            history_cat = [self.vocab.eos_id, self.vocab.eos_id]
        profile = profile[:48]
        history_cat = history_cat[-128:]
        contexts = [torch.tensor([c], dtype=torch.long, device=self.device) for c in [profile, history_cat] if len(c) > 0]
        with torch.no_grad():
            enc_contexts = [self.transformer.encode(c) for c in contexts]
            partial_out_ids = self.vocab.string2ids(' '.join(''.join(partial_out)))
            prediction = self.transformer.predict_next(enc_contexts, prefix=partial_out_ids)
        # distribute = {self.vocab.id2token[i]: t for i, t in enumerate(prediction) if t > 0 and i != self.vocab.eos_id}
        eos_prob = prediction[self.vocab.eos_id]
        distribute = {self.vocab.id2token[i]: max(t, 1e-8) for i, t in enumerate(prediction)}
        return distribute, eos_prob

    def gen_response(self, contexts):
        """
        Return a list of responses to each context.

        :param contexts: list, a list of context, each context is a dict that contains the dialogue history and personal
                         profile of each speaker
                         this dict contains following keys:

                         context['dialog']: a list of string, dialogue histories (tokens in each utterances
                                            are separated using spaces).
                         context['uid']: a list of int, indices to the profile of each speaker
                         context['profile']: a list of dict, personal profiles for each speaker
                         context['responder_profile']: dict, the personal profile of the responder

        :return: list, responses for each context, each response is a list of tokens.

        e.g.
        contexts:
        [{ "dialog": [ ["How are you ?"], ["I am fine , thank you . And you ?"] ],
          "uid": [0, 1],
          "profile":[ { "loc":"Beijing", "gender":"male", "tag":"" },
                      { "loc":"Shanghai", "gender":"female", "tag":"" } ],
          "responder_profile":{ "loc":"Beijing", "gender":"male", "tag":"" }
        }]

        ==>  [['I', 'am', 'fine', 'too', '!']]
        """
        res = []
        for context in contexts:
            if 'responder_profile' in context:
                responder_profile = context['responder_profile']
                tag = responder_profile['tag'].replace(' ', '')
            else:
                responder_profile = context['response_profile']
                tag = ';'.join(responder_profile['tag']).replace(' ', '')
            dialog = context['dialog']
            loc = ';'.join(responder_profile['loc'].split()).replace(' ', '')
            gender = '男' if responder_profile['gender'] == 'male' else '女'
            persona = '性别:' + gender + ',' + '地点:' + loc + ',' + '标签:' + tag
            profile_ids = self.vocab.string2ids(' '.join(persona))
            dialog_ids = [self.vocab.string2ids(' '.join(i[0].replace(' ', ''))) for i in dialog]
            profile = [self.vocab.eos_id] + profile_ids + [self.vocab.eos_id]
            if len(dialog_ids):
                history_cat = [self.vocab.eos_id]
                for i in dialog_ids[:-1]:
                    history_cat.extend(i)
                    history_cat.extend([self.vocab.spl_id])
                history_cat.extend(dialog_ids[-1])
                history_cat.extend([self.vocab.eos_id])
            else:
                history_cat = [self.vocab.eos_id, self.vocab.eos_id]
            profile = profile[:48]
            history_cat = history_cat[-128:]
            contexts = [torch.tensor([c], dtype=torch.long, device=self.device) for c in [profile, history_cat] if
                        len(c) > 0]
            with torch.no_grad():
                prediction = self.transformer.predict(contexts)[0]
            # prediction_str = [self.vocab.id2token[id] for id in prediction]
            prediction_str = self.vocab.ids2string(prediction)
            res.append(list(prediction_str))
        return res

def test(model, input_file, output_file):
    with open(output_file, 'w', encoding='utf8') as fw:
        with open(input_file, 'r', encoding='utf8') as fr:
            lines = fr.readlines()
            for line in lines[:100]:
                line = line.strip('\n')
                data = json.loads(line)
                dialog = data['dialog']
                uid = data['uid']
                profile_all = data['profile']
                if 'responder_profile' in data:
                    responder_profile = data['responder_profile']
                else:
                    responder_profile = data['response_profile']
                golden_response = data['golden_response']
                golden_response_str = ''.join(golden_response).replace(' ', '')
                dialog_str = '\n\t'.join([''.join(i).replace(' ', '') for i in dialog])
                profile_all_str = '\n\t'.join([json.dumps(i, ensure_ascii=False) for i in profile_all])
                responder_profile_str = json.dumps(responder_profile, ensure_ascii=False)
                fw.write('all profiles: \n\t' + profile_all_str + '\n')
                fw.write('responder profile: \n\t' + responder_profile_str + '\n')
                fw.write('history: \n\t' + dialog_str + '\n')
                fw.write('golden response: \n\t' + golden_response_str + '\n')
                ans_auto = model.gen_response([data])
                ans_auto = ''.join(ans_auto[0])
                fw.write('predict with lost in conversation: ' + ans_auto + '\n')
                fw.write('\n')

if __name__ == '__main__':
    model = Model()
    # test_biased(model)
    files = [['data/test_data_biased.json', 'data/test_data_biased_lost.txt'],
             ['data/test_data_random.json', 'data/test_data_random_lost.txt']]
    test(model, files[0][0], files[0][1])
    test(model, files[1][0], files[1][1])
