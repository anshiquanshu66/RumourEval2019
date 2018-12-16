import logging
import math
import time

import torch
from torch.nn.modules.loss import _Loss
from torchtext.data import BucketIterator, Iterator
from tqdm import tqdm

from RumourEvalDataset_Branches import RumourEval2019Dataset_Branches
from modelutils import glorot_param_init
from utils import count_parameters, get_timestamp

__author__ = "Martin Fajčík"

step = 0


# FIXME: learn special embedding tokens
# RNN baseline (Best 2.1256|0.854494) drop 0.6, FC 2, fc_size 300
# No RNN base (Best 2.1018|0.857347)
# textonly  (Best 3.6824|0.690799)
# textonly
# textonly + embopt (Best 3.9955|0.676177)

# textonly 0.714337 after text preprocessing
class Base_Framework:
    def __init__(self, config: dict):
        self.config = config
        self.save_treshold = 0.855

    def build_dataset(self, path, fields):
        return RumourEval2019Dataset_Branches(path, fields), {k: v for k, v in fields}

    def train(self, modelfunc):
        config = self.config

        fields = RumourEval2019Dataset_Branches.prepare_fields(self.config["hyperparameters"]["sep_token"])
        train_data, train_fields = self.build_dataset(config["train_data"], fields)
        dev_data, dev_fields = self.build_dataset(config["dev_data"], fields)

        torch.manual_seed(42)

        # No need to build vocab for baseline
        # but fo future work I wrote RumourEval2019Dataset that
        # requires vocab to be build
        build_vocab = lambda field, *data: RumourEval2019Dataset_Branches.build_vocab(field,
                                                                                      self.config["hyperparameters"][
                                                                                 "sep_token"],
                                                                                      *data,
                                                                                      vectors=config["embeddings"],
                                                                                      vectors_cache=config[
                                                                                          "vector_cache"])
        # FIXME: add dev vocab to model's vocab
        # 14833 together
        # 11809 words in train
        # 6450 words in dev
        build_vocab(train_fields['spacy_processed_text'], train_data, dev_data)
        self.vocab = train_fields['spacy_processed_text'].vocab

        device = torch.device("cuda:0" if config['cuda'] and
                                          torch.cuda.is_available() else "cpu")

        create_iter = lambda data: BucketIterator(data, sort_key=lambda x: len(x.stance_labels), sort=True,
                                                  batch_size=config["hyperparameters"]["batch_size"], repeat=False,
                                                  device=device)
        train_iter = create_iter(train_data)
        dev_iter = create_iter(dev_data)

        logging.info(f"Train examples: {len(train_data.examples)}\nValidation examples: {len(dev_data.examples)}")

        model = modelfunc(self.config, vocab=train_fields['spacy_processed_text'].vocab).to(device)

        glorot_param_init(model)
        logging.info(f"Model has {count_parameters(model)} trainable parameters.")
        logging.info(f"Manual seed {torch.initial_seed()}")
        optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad, model.parameters()),
                                     lr=config["hyperparameters"]["learning_rate"],
                                     betas=[0.9, 0.999], eps=1e-8)
        lossfunction = torch.nn.CrossEntropyLoss()
        start_time = time.time()
        try:
            best_val_loss = math.inf
            best_val_acc = 0
            for epoch in range(config["hyperparameters"]["epochs"]):
                train_loss, train_acc = self.run_epoch(model, lossfunction, optimizer, train_iter, config)
                validation_loss, validation_acc = self.validate(model, lossfunction, dev_iter, config)
                if validation_loss < best_val_loss:
                    best_val_loss = validation_loss
                if validation_acc > best_val_acc:
                    best_val_acc = validation_acc
                logging.info(
                    f"Epoch {epoch}, Validation loss|acc: {validation_loss:.6f}|{validation_acc:.6f} - (Best {best_val_loss:.4f}|{best_val_acc:4f})")
                if validation_acc > self.save_treshold:
                    torch.save(model,
                               f"saved/checkpoint_{str(self.__class__)}_ACC_{validation_acc:.5f}_{get_timestamp()}.pt")
        except KeyboardInterrupt:
            logging.info('-' * 120)
            logging.info('Exit from training early.')
        finally:
            logging.info(f'Finished after {(time.time() - start_time) / 60} minutes.')

    def run_epoch(self, model, lossfunction, optimizer, train_iter, config, verbose=False):
        total_batches = len(train_iter.data()) // train_iter.batch_size
        if verbose:
            pbar = tqdm(total=total_batches)
        examples_so_far = 0
        train_loss = 0
        total_correct = 0
        for i, batch in enumerate(train_iter):
            optimizer.zero_grad()
            pred_logits = model(batch)

            pred_logits = pred_logits.view((-1, 4))
            labels = batch.stance_labels.view(-1)
            mask = labels > -1
            masked_preds, masked_labels = pred_logits[mask, :], labels[mask]
            loss = lossfunction(masked_preds, masked_labels)

            loss.backward()
            if model.encoder is not None:
                torch.nn.utils.clip_grad_norm_(model.encoder.parameters(), config["hyperparameters"]["RNN_clip"])
            optimizer.step()

            total_correct += self.calculate_correct(masked_preds, masked_labels)
            examples_so_far += 1
            train_loss += loss.item()
            if verbose:
                pbar.set_description(
                    f"Train loss: {train_loss / (i + 1):.4f}, Train acc: {total_correct / examples_so_far:.4f}")
                pbar.update(1)
        return train_loss / train_iter.batch_size, total_correct / examples_so_far

    def validate(self, model: torch.nn.Module, lossfunction: _Loss, dev_iter: Iterator, config: dict, verbose=False):
        train_flag = model.training
        model.eval()

        total_batches = len(dev_iter.data()) // dev_iter.batch_size
        if verbose:
            pbar = tqdm(total=total_batches)
        examples_so_far = 0
        dev_loss = 0
        total_correct = 0
        for i, batch in enumerate(dev_iter):
            predictions, maxpreds, argmaxpreds, attention = model.predict(batch)

            pred_logits = pred_logits.view((-1, 4))
            labels = batch.stance_labels.view(-1)
            mask = labels > -1
            masked_preds, masked_labels = pred_logits[mask, :], labels[mask]
            loss = lossfunction(masked_preds, masked_labels)

            total_correct += self.calculate_correct(masked_preds, masked_labels)
            examples_so_far += len(masked_labels)
            dev_loss += loss.item()
            if verbose:
                pbar.set_description(
                    f"dev loss: {dev_loss / (i + 1):.4f}, dev acc: {total_correct / examples_so_far:.4f}")
                pbar.update(1)
        if train_flag:
            model.train()
        return dev_loss / dev_iter.batch_size, total_correct / examples_so_far

    def calculate_correct(self, pred_logits: torch.Tensor, labels: torch.Tensor):
        preds = torch.argmax(pred_logits, dim=1)
        return torch.sum(preds == labels).item()