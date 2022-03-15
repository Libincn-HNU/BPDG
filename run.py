#!/usr/bin/env python3
# coding:utf-8

# Copyright (c) Tsinghua university conversational AI group (THU-coai).
# This source code is licensed under the MIT license.
import codecs
import warnings
warnings.filterwarnings("ignore")
import torch
import random
from model.utils import load_openai_weights_chinese, set_seed
from model.transformer_context_model import TransformerContextModel
from model.text import myVocab
from config import get_model_config_context, get_test_config_context
from collections import Counter
import json


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
        model_config = get_model_config_context()
        test_config = get_test_config_context()
        set_seed(test_config.seed)
        device = torch.device(test_config.device)
        vocab = myVocab(model_config.vocab_path)
        self.vocab = vocab
        transformer = TransformerContextModel(n_layers=model_config.n_layers,
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
        load_last = False
        if not load_last:
            openai_model = torch.load(model_config.openai_parameters_dir, map_location=device)
            # openai_model.pop('decoder.pre_softmax.weight')
            b = list(openai_model.keys())
            # print(b)
            model_dict = {}
            for idx1, key1 in enumerate(transformer.transformer_module.state_dict()):
                try:
                    for idx2, key2 in enumerate(openai_model):
                        if idx1 == idx2:
                            if transformer.transformer_module.state_dict()[key1].shape != openai_model[key2].shape:
                                model_dict[key1] = openai_model[key2].transpose(1, 0)
                            else:
                                model_dict[key1] = openai_model[key2]
                except:
                    pass
            assert (len(transformer.transformer_module.state_dict().keys()) == len(model_dict))
            transformer.transformer_module.load_state_dict(model_dict)
            print('OpenAI weights chinese loaded from {}'.format(model_config.openai_parameters_dir))
        else:
            ##TODO make sure load valid
            state_dict = torch.load(test_config.last_checkpoint_path, map_location=device)
            transformer.load_state_dict(state_dict['model'])
        transformer.eval()
        self.model_config = model_config
        self.test_config = test_config
        self.transformer = transformer
        self.device = device
        # print('Weights loaded from {}'.format(test_config.last_checkpoint_path))

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
        if 'responder_profile' in context:
            responder_profile = context['responder_profile']
        else:
            responder_profile = context['response_profile']
        dialog = context['dialog']
        tag = ';'.join(responder_profile['tag']).replace(' ', '')
        loc = ';'.join(responder_profile['loc'].split()).replace(' ', '')
        gender = '男' if responder_profile['gender'] == 'male' else '女'
        persona = '标签:' + tag + ',' + '地点:' + loc + ',' + '性别:' + gender
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
        profile = profile[:64]
        history_cat = history_cat[-256:]
        contexts = [torch.tensor([c], dtype=torch.long, device=self.device) for c in [profile, history_cat] if
                    len(c) > 0]
        with torch.no_grad():
            enc_contexts = [self.transformer.encode(c) for c in contexts]
            partial_out_ids = self.vocab.string2ids(' '.join(''.join(partial_out)))
            prediction = self.transformer.predict_next(enc_contexts, prefix=partial_out_ids)
        distribute = {self.vocab.id2token[i]: t for i, t in enumerate(prediction) if t > 0 and i != self.vocab.eos_id}
        eos_prob = prediction[self.vocab.eos_id]
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
            persona = '标签:' + tag + ',' + '地点:' + loc + ',' + '性别:' + gender
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
            profile = profile[:64]
            history_cat = history_cat[-256:]
            contexts = [torch.tensor([c], dtype=torch.long, device=self.device) for c in [profile, history_cat] if
                        len(c) > 0]
            with torch.no_grad():
                prediction = self.transformer.predict(contexts)[0]
            prediction_str = self.vocab.ids2string(prediction)
            profile_str = self.vocab.ids2string(profile)
            history_cat_str = self.vocab.ids2string(history_cat)
            # prediction_str = [self.vocab.id2token[id] for id in prediction]
            res.append([profile_str, history_cat_str, prediction_str])
        return res


if __name__ == '__main__':
    model = Model()
    import tqdm
    with open('data/test_data_random_predict_second174.txt', 'w', encoding='utf8') as fw:
        with open('data/test_data_random.json', 'r', encoding='utf8') as fr:
            lines = fr.readlines()
            for line in tqdm.tqdm(lines):
                context = json.loads(line.strip('\n'))
                profile, history, predict = model.gen_response([context])[0]
                fw.write('persona: ' + profile + '\n')
                fw.write('history: ' + history + '\n')
                fw.write('predict: ' + predict + '\n')
                fw.write('\n')
