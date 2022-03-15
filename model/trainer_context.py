#  transformer_chatbot
#  Copyright (C) 2018 Golovanov, Tselousov
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.

import torch
import torch.nn as nn
import torch.nn.functional as F
import random
from torch.utils.data import DataLoader
from tqdm import tqdm
from .utils import pad_sequence
from .optim import Adam, NoamOpt
from .loss import LabelSmoothingLoss
import json
import logging

logger = logging.getLogger('s2s-smp')
logger.setLevel(logging.INFO)
fh = logging.FileHandler('s2s-smp.log', encoding='utf-8')
fh.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
fh.setFormatter(formatter)
ch.setFormatter(formatter)
logger.addHandler(fh)
logger.addHandler(ch)
# 记录一条日志
logger.info('python logging test')


class TrainerContext:
    def __init__(self, model, train_dataset, test_dataset=None, batch_size=8,
                 batch_split=1, lm_weight=0.5, risk_weight=0, lr=6.25e-5, lr_warmup=2000,
                 n_jobs=0, clip_grad=None, label_smoothing=0, device=torch.device('cuda'),
                 ignore_idxs=[]):
        self.model = model.to(device)
        self.lm_criterion = nn.CrossEntropyLoss(ignore_index=self.model.padding_idx).to(device)
        self.criterion = LabelSmoothingLoss(n_labels=self.model.n_embeddings, smoothing=label_smoothing,
                                            ignore_index=self.model.padding_idx).to(device)
        # base_optimizer = Adam(self.model.parameters(), lr=lr, weight_decay=0.01)
        # self.optimizer = NoamOpt(self.model.embeddings_size, 0.1, lr_warmup, base_optimizer)
        #         for p in self.model.parameters():
        #             print(p)

        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=lr, weight_decay=0.01)
        self.scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(self.optimizer, T_max=500, eta_min=2.5e-8)
        # self.scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, 1, gamma=0.8, last_epoch=-1)
        # self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer, mode='min', factor=0.1, patience=2,
        #                                                             verbose=True, min_lr=1e-8)

        # self.train_dataloader = DataLoader(train_dataset, batch_size=batch_size // batch_split, shuffle=True,
        #                                    num_workers=n_jobs, collate_fn=self.collate_func)
        self.train_dataloader = DataLoader(train_dataset, batch_size=batch_size // batch_split, shuffle=True,
                                           num_workers=0, collate_fn=self.collate_func)

        if test_dataset is not None:
            self.test_dataloader = DataLoader(test_dataset, batch_size=batch_size // batch_split, shuffle=False,
                                              num_workers=n_jobs, collate_fn=self.collate_func)

        self.batch_split = batch_split
        self.lm_weight = lm_weight
        self.risk_weight = risk_weight
        self.clip_grad = clip_grad
        self.device = device
        self.ignore_idxs = ignore_idxs

    def state_dict(self):
        return {'model': self.model.state_dict(),
                'optimizer': self.optimizer.state_dict()}

    def load_state_dict(self, state_dict):
        self.model.load_state_dict(state_dict['model'], strict=False)
        self.optimizer.load_state_dict(state_dict['optimizer'])

    def collate_func(self, data):
        persona_info, h, y = zip(*data)
        """
        from model.text import myVocab
        from config import get_model_config_context
        model_config = get_model_config_context()
        vocab = myVocab(model_config.vocab_path)
        vocab.ids2string(profile[1:-1])
        """

        contexts = []

        if max(map(len, persona_info)) > 0:
            persona_info = [torch.tensor(d, dtype=torch.long) for d in persona_info]
            persona_info = pad_sequence(persona_info, batch_first=True, padding_value=self.model.padding_idx)
            contexts.append(persona_info)

        if max(map(len, h)) > 0:
            h = [torch.tensor(d, dtype=torch.long) for d in h]
            h = pad_sequence(h, batch_first=True, padding_value=self.model.padding_idx)
            contexts.append(h)

        y = [torch.tensor(d, dtype=torch.long) for d in y]
        y = pad_sequence(y, batch_first=True, padding_value=self.model.padding_idx)

        return contexts, y###personal_info 与 history 放一起了 作为 context  然后下一句做为ground truth

    def _eval_train(self, epoch, risk_func=None):
        self.model.train()

        tqdm_data = tqdm(self.train_dataloader, desc='Train (epoch #{})'.format(epoch))
        loss = 0
        lm_loss = 0
        risk_loss = 0
        for i, (contexts, targets) in enumerate(tqdm_data):
            contexts, targets = [c.to(self.device) for c in contexts], targets.to(self.device)

            enc_contexts = []

            # lm loss
            batch_lm_loss = torch.tensor(0, dtype=torch.float, device=self.device)
            for context in contexts:
                enc_context = self.model.encode(context.clone())
                enc_contexts.append(enc_context)

                if self.lm_weight > 0:##TODO check the meaning
                    context_outputs = self.model.generate(enc_context[0])##将输出放入pre_softmax中，这里得到的是logits，为lm头
                    ignore_mask = torch.stack([context == idx for idx in self.ignore_idxs], dim=-1).any(dim=-1)##batch_size, max_len
                    context.masked_fill_(ignore_mask, self.model.padding_idx)##shift right 操作
                    prevs, nexts = context_outputs[:, :-1, :].contiguous(), context[:, 1:].contiguous()
                    batch_lm_loss += (
                            self.lm_criterion(prevs.view(-1, prevs.shape[-1]), nexts.view(-1)) / len(contexts))

            # s2s loss
            prevs, nexts = targets[:, :-1].contiguous(), targets[:, 1:].contiguous()
            outputs = self.model.decode(prevs, enc_contexts)
            outputs = F.log_softmax(outputs, dim=-1)
            batch_loss = self.criterion(outputs.view(-1, outputs.shape[-1]), nexts.view(-1))##soft label

            # risk loss
            batch_risk_loss = torch.tensor(0, dtype=torch.float, device=self.device)
            if risk_func is not None and self.risk_weight > 0:##计算f1??

                beams, beam_lens = self.model.beam_search(enc_contexts, return_beams=True)

                target_lens = targets.ne(self.model.padding_idx).sum(dim=-1)
                targets = [target[1:length - 1].tolist() for target, length in zip(targets, target_lens)]
                batch_risks = []
                for b in range(beams.shape[1]):
                    predictions = [b[1:l - 1].tolist() for b, l in zip(beams[:, b, :], beam_lens[:, b])]
                    risks = torch.tensor(risk_func(predictions, targets), dtype=torch.float, device=self.device)
                    batch_risks.append(risks)
                batch_risks = torch.stack(batch_risks, dim=-1)

                batch_probas = []
                for b in range(beams.shape[1]):
                    logits = self.model.decode(beams[:, b, :-1], enc_contexts)
                    probas = F.log_softmax(logits, dim=-1)
                    probas = torch.gather(probas, -1, beams[:, b, 1:].unsqueeze(-1)).squeeze(-1)
                    probas = probas.sum(dim=-1) / beam_lens[:, b].float()
                    batch_probas.append(probas)
                batch_probas = torch.stack(batch_probas, dim=-1)
                batch_probas = F.softmax(batch_probas, dim=-1)

                batch_risk_loss = torch.mean((batch_risks * batch_probas).sum(dim=-1))

            # optimization
            full_loss = (batch_lm_loss * self.lm_weight + self.risk_weight * batch_risk_loss + batch_loss) / self.batch_split
            full_loss.backward()

            if (i - 1) % self.batch_split == 0:
                if self.clip_grad is not None:
                    for group in self.optimizer.param_groups:
                        nn.utils.clip_grad_norm_(group['params'], self.clip_grad)

                self.optimizer.step()
                # self.scheduler.step()
                self.optimizer.zero_grad()
            lm_loss = (i * lm_loss + batch_lm_loss.item()) / (i + 1)
            loss = (i * loss + batch_loss.item()) / (i + 1)
            risk_loss = (i * risk_loss + batch_risk_loss.item()) / (i + 1)
            # tqdm_data.set_postfix({'lm_loss': lm_loss, 'loss': loss, 'risk_loss': risk_loss, 'loss_step': batch_loss.item(),
            #                        'lr': self.optimizer.rate(), 'step': self.optimizer._step})
            tqdm_data.set_postfix(
                {'lm_loss': lm_loss, 'loss': loss, 'risk_loss': risk_loss, 'loss_step': batch_loss.item(),
                 'lr': self.optimizer.state_dict()['param_groups'][0]['lr'], 'step': i})
            # break
        # log_dict = {'epoch': epoch, 'lm_loss': lm_loss, 'loss': loss, 'risk_loss': risk_loss,
        #             'lr': self.optimizer.rate(), 'step': self.optimizer._step}

        log_dict = {'epoch': epoch, 'lm_loss': lm_loss, 'loss': loss, 'risk_loss': risk_loss,
                    'lr': self.optimizer.state_dict()['param_groups'][0]['lr'], 'step': i}
        # log_dict = {'epoch': epoch, 'lm_loss': lm_loss, 'loss': loss, 'risk_loss': risk_loss,
        #             'lr': self.optimizer.rate(), 'step': self.optimizer._step}

        log_dict_json = json.dumps(log_dict, ensure_ascii=False)
        logger.info(log_dict_json)

    def _eval_test(self, metric_funcs={}):
        self.model.eval()

        tqdm_data = tqdm(self.test_dataloader, desc='Test')
        loss = 0
        lm_loss = 0
        metrics = {name: 0 for name in metric_funcs.keys()}
        for i, (contexts, targets) in enumerate(tqdm_data):
            contexts, targets = [c.to(self.device) for c in contexts], targets.to(self.device)

            enc_contexts = []

            # lm loss
            batch_lm_loss = torch.tensor(0, dtype=torch.float, device=self.device)
            for context in contexts:
                enc_context = self.model.encode(context.clone())
                enc_contexts.append(enc_context)

                if self.lm_weight > 0:
                    context_outputs = self.model.generate(enc_context[0])
                    ignore_mask = torch.stack([context == idx for idx in self.ignore_idxs], dim=-1).any(dim=-1)
                    context.masked_fill_(ignore_mask, self.model.padding_idx)
                    prevs, nexts = context_outputs[:, :-1, :].contiguous(), context[:, 1:].contiguous()
                    batch_lm_loss += (
                            self.lm_criterion(prevs.view(-1, prevs.shape[-1]), nexts.view(-1)) / len(contexts))

            # s2s loss
            prevs, nexts = targets[:, :-1].contiguous(), targets[:, 1:].contiguous()
            outputs = self.model.decode(prevs, enc_contexts)
            outputs = F.log_softmax(outputs, dim=-1)
            batch_loss = self.criterion(outputs.view(-1, outputs.shape[-1]), nexts.view(-1))

            predictions = self.model.beam_search(enc_contexts)
            target_lens = targets.ne(self.model.padding_idx).sum(dim=-1)
            targets = [t[1:l - 1].tolist() for t, l in zip(targets, target_lens)]

            lm_loss = (i * lm_loss + batch_lm_loss.item()) / (i + 1)
            loss = (i * loss + batch_loss.item()) / (i + 1)
            for name, func in metric_funcs.items():
                score = func(predictions, targets)
                metrics[name] = (metrics[name] * i + score) / (i + 1)

            tqdm_data.set_postfix(dict({'lm_loss': lm_loss, 'loss': loss}, **metrics))
        log_dict = dict({'lm_loss': lm_loss, 'loss': loss}, **metrics)
        log_dict_json = json.dumps(log_dict, ensure_ascii=False)
        logger.info(log_dict_json)

    def test(self, metric_funcs={}):
        if hasattr(self, 'test_dataloader'):
            self._eval_test(metric_funcs)

    # def train(self, start_epoch, epochs, after_epoch_funcs=[], risk_func=None):
    #     for epoch in range(start_epoch, epochs):
    #         self._eval_train(epoch, risk_func)
    #         # if epoch % 1 == 0 and epoch > 0:
    #         for func in after_epoch_funcs:
    #             func(epoch)
    def train(self, start_epoch, epochs, after_epoch_funcs=[], risk_func=None):
        for epoch in range(start_epoch, epochs):
            self._eval_train(epoch, risk_func)
            if epoch % 10 == 0 and epoch > 0:
                for func in after_epoch_funcs:
                    func(epoch)