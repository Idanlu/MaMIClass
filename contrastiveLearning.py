import os
import torch
from torch.nn import DataParallel
from tqdm import tqdm
import logging
import numpy as np
import sys

class CLModel:

    def __init__(self, net, loss, loader_train, loader_val, config, scheduler=None):
        """

        Parameters
        ----------
        net: subclass of nn.Module
        loss: callable fn with args (y_pred, y_true)
        loader_train, loader_val: pytorch DataLoaders for training/validation
        config: Config object with hyperparameters
        scheduler (optional)
        """
        super().__init__()
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        self.logger = logging.getLogger("yAwareCL")
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG)
        self.loss = loss
        self.model = net
        self.optimizer = torch.optim.Adam(net.parameters(), lr=config.lr, weight_decay=config.weight_decay)
        self.scheduler = scheduler
        self.loader = loader_train
        self.loader_val = loader_val
        self.device = torch.device("cuda" if config.cuda else "cpu")
        if config.cuda and not torch.cuda.is_available():
            raise ValueError("No GPU found: set cuda=False parameter.")
        self.config = config
        self.metrics = {}

        self.model = DataParallel(self.model).to(self.device)

        if hasattr(config, 'pretrained_path') and config.pretrained_path is not None:
            self.load_model(config.pretrained_path)

    def pretraining(self):
        print(self.loss)
        print(self.optimizer)
        losses = {'train':[], 'validation':[]}
        print("Started Pretraining")

        for epoch in range(self.config.nb_epochs):

            ## Training step
            self.model.train()
            nb_batch = len(self.loader)
            training_loss = 0
            pbar = tqdm(total=nb_batch, desc="Training")
            for (inputs, labels, paths) in self.loader:
                pbar.update()
                inputs = inputs.to(self.device)
                labels = labels.to(self.device)
                self.optimizer.zero_grad()

                z_i = self.model(inputs[:, 0, :])
                z_j = self.model(inputs[:, 1, :])
                if self.config.loss == 'NTXent':
                    batch_loss, logits, target = self.loss(z_i, z_j)
                else:
                    z = torch.stack((z_i, z_j), dim=1)
                    batch_loss = self.loss(z, labels)

                batch_loss.backward()
                self.optimizer.step()
                training_loss += float(batch_loss) / nb_batch
            pbar.close()
            losses['train'].append(training_loss)

            ## Validation step
            nb_batch = len(self.loader_val)
            pbar = tqdm(total=nb_batch, desc="Validation")
            val_loss = 0
            val_values = {}
            with torch.no_grad():
                self.model.eval()
                for (inputs, labels, paths) in self.loader_val:
                    pbar.update()
                    inputs = inputs.to(self.device)
                    labels = labels.to(self.device)

                    z_i = self.model(inputs[:, 0, :])
                    z_j = self.model(inputs[:, 1, :])
                    if self.config.loss == 'NTXent':
                        batch_loss, logits, target = self.loss(z_i, z_j)
                    else:
                        z = torch.stack((z_i, z_j), dim=1)
                        batch_loss = self.loss(z, labels)

                    val_loss += float(batch_loss) / nb_batch
                    for name, metric in self.metrics.items():
                        if name not in val_values:
                            val_values[name] = 0
                        val_values[name] += metric(logits, target) / nb_batch
            pbar.close()
            losses['validation'].append(val_loss)

            metrics = "\t".join(["Validation {}: {:.4f}".format(m, v) for (m, v) in val_values.items()])
            print("Epoch [{}/{}] Training loss = {:.4f}\t Validation loss = {:.4f}\t".format(
                epoch+1, self.config.nb_epochs, training_loss, val_loss)+metrics, flush=True)

            if self.scheduler is not None:
                self.scheduler.step()

            if (epoch % self.config.nb_epochs_per_saving == 0 or epoch == self.config.nb_epochs - 1) and epoch > 0:
                torch.save({
                    "epoch": epoch+1,
                    "model": self.model.state_dict(),
                    "optimizer": self.optimizer.state_dict(),
                    "losses": losses},
                    os.path.join(self.config.checkpoint_dir, "{name}_epoch_{epoch}.pth".
                                 format(name=self.config.loss, epoch=epoch+1)))


    def fine_tuning(self):
        print(self.loss)
        print(self.optimizer)
        losses = {'train':[], 'validation':[]}

        # freeze all layers except the classification layer
        # for name, param in self.model.named_parameters():
        #     if 'classifier' not in name:
        #         param.requires_grad = False

        for epoch in range(self.config.nb_epochs):
            ## Training step
            self.model.train()
            nb_batch = len(self.loader)
            training_loss = 0
            pbar = tqdm(total=nb_batch, desc="Training")
            for (inputs, labels, paths) in self.loader:
                pbar.update()
                inputs = inputs.to(self.device)
                labels = labels.to(self.device)
                self.optimizer.zero_grad()
                y = self.model(inputs)
                batch_loss = self.loss(y,labels)
                batch_loss.backward()
                self.optimizer.step()
                training_loss += float(batch_loss.item()) / nb_batch
            pbar.close()
            losses['train'].append(training_loss)

            ## Validation step
            nb_batch = len(self.loader_val)
            pbar = tqdm(total=nb_batch, desc="Validation")
            val_loss = 0
            with torch.no_grad():
                self.model.eval()
                for (inputs, labels, paths) in self.loader_val:
                    pbar.update()
                    inputs = inputs.to(self.device)
                    labels = labels.to(self.device)
                    y = self.model(inputs)
                    batch_loss = self.loss(y, labels)
                    val_loss += float(batch_loss) / nb_batch
            pbar.close()
            losses['validation'].append(val_loss)

            print("Epoch [{}/{}] Training loss = {:.4f}\t Validation loss = {:.4f}\t".format(
                epoch+1, self.config.nb_epochs, training_loss, val_loss), flush=True)

            if self.scheduler is not None:
                self.scheduler.step()

            if (epoch % self.config.nb_epochs_per_saving == 0 or epoch == self.config.nb_epochs - 1) and epoch > 0:
                torch.save({
                    "epoch": epoch+1,
                    "model": self.model.state_dict(),
                    "optimizer": self.optimizer.state_dict(),
                    "losses": losses},
                    f"{self.config.checkpoint_dir}/fine_tune_epoch_{epoch+1}.pth")
    
    def load_checkpoint(self, state_dict):
        model_state_dict = self.model.state_dict()
        for k in state_dict:
            if k in model_state_dict:
                if state_dict[k].shape != model_state_dict[k].shape:
                    self.logger.info(f"Skip loading parameter: {k}, "
                                f"required shape: {model_state_dict[k].shape}, "
                                f"loaded shape: {state_dict[k].shape}")
                    state_dict[k] = model_state_dict[k]
                    is_changed = True
            else:
                self.logger.info(f"Dropping parameter {k}")
                is_changed = True

        self.model.load_state_dict(state_dict, strict=False)

    def load_model(self, path):
        checkpoint = None
        self.logger.debug("Loading model")
        try:
            checkpoint = torch.load(path, map_location=lambda storage, loc: storage)
        except BaseException as e:
            self.logger.error('Impossible to load the checkpoint: %s' % str(e))
        if checkpoint is not None:
            try:
                if hasattr(checkpoint, "state_dict"):
                    unexpected = self.model.load_state_dict(checkpoint.state_dict())
                    self.logger.info('Model loading info: {}'.format(unexpected))
                elif isinstance(checkpoint, dict):
                    if "model" in checkpoint:
                        unexpected = self.load_checkpoint(checkpoint["model"])
                        self.logger.info('Model loading info: {}'.format(unexpected))
                else:
                    unexpected = self.model.load_state_dict(checkpoint)
                    self.logger.info('Model loading info: {}'.format(unexpected))
            except BaseException as e:
                raise ValueError('Error while loading the model\'s weights: %s' % str(e))





