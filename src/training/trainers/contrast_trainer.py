import dataclasses
from src.training.trainers import base
from torch.utils.data import dataset
from src.training.callbacks import (
    checkpoints,
    devices,
    early_stopping,
    logistics,
    distributed as call_dist
)
import numpy
import pathlib
from src.training import exceptions
from torch.distributed.optim import zero_redundancy_optimizer as zero
from torch.nn.parallel import DistributedDataParallel as DDP
import os
import random
from tqdm import tqdm
import typing
from torch import nn
from torch.utils import data
from src.training.contrastive_learning import sampler
import torch
from torch import optim
from torch.optim import lr_scheduler
from torch import device

@dataclasses.dataclass
class TrainerConfig(object):
    """
    Configuration instance, that is responsible
    for training single network instance.
    """
    network: nn.Module
    train_devices: typing.List[torch.DeviceObjType]
    optimizer_config: typing.Dict[str, typing.Any]
    lr_scheduler_config: typing.Dict[str, typing.Any]

class ContrastiveTrainer(base.BaseTrainer):
    """
    Training pipeline for contrastive learning
    of multiple embedding generation networks:

        - Autoencoder-backed image embedding generator for processing image data.
        - DistilBERT-backed word embedding generator for processing text data.

    Parameters:
    -----------
        networks: list of embedding generation networks for each modality.
        optimizers: list of optimizers for each embedding generator.
        lr_schedulers: list of LR schedulers for each embedding generator.
        contrastive_sampler: sampler for hard mining sample pairs.
        batch_size: int - size of the data batch, feed to networks at each iteration
        distributed: bool - enable distributed training.
    """
    def __init__(self,
        train_configs: typing.List[TrainerConfig],
        contrastive_sampler: sampler.BaseSampler,
        batch_size: int,
        pair_loss_name: str,
        modal_loss_name: str,
        eval_metric_name: str,
        log_dir: typing.Union[str, pathlib.Path],
        distributed: bool = False,
        dist_rank: int = None,
        dist_backend: typing.Literal["nccl", "golo"] = None,
        world_size: int = None,
        group_name: str = None,
        reproducible: bool = False
    ):
        super(ContrastiveTrainer, self).__init__()

        self.contrastive_sampler = contrastive_sampler
        self.batch_size = batch_size
        self.distributed = distributed 
        self.reproducible = reproducible
        self.world_size = world_size 
        self.dist_rank = dist_rank 
        self.dist_backend = dist_backend
        self.group_name = group_name

        self.configure_callbacks(base_log_dir=log_dir)

        self.on_init_start()
        self.configure_setup(train_configs=train_configs)

        # two loss functions for contrasive learning training.
        # 1. loss for measuring similarity / dissimilarity between hard negative and positive pairs
        # 2. loss for measuring similarity between embeddings from multiple modalities

        self.pair_loss_function = self.load_loss(pair_loss_name)
        self.modal_loss_function = self.load_loss(modal_loss_name)
        self.eval_metric = self.load_metric(eval_metric_name)
        self.stop = False # status code to urgently stop training

    def configure_setup(self, train_configs: typing.List[TrainerConfig]):
        """
        Configures networks, optimization algorithms and
        learning rate schedulers.
        
        Parameters:
        -----------
            train_configs - list of TrainerConfig objects
        """
        self.networks = []
        self.optimizers = []
        self.schedulers = []

        for config in train_configs:
            network = self.configure_network(
                network=config.network,
                device_ids=config.train_devices,
                output_device=config.output_device
            )
            optimizer = self.configure_optimizer(
                network=network,
                optimizer_config=config.optimizer_config
            )
            lr_scheduler = self.configure_lr_scheduler(
                optimizer=optimizer,
                lr_scheduler_config=config.lr_scheduler_config
            )
            self.networks.append(network)
            self.optimizers.append(optimizer)
            self.schedulers.append(lr_scheduler)
        
    def configure_network(self, 
        network: nn.Module, 
        device_ids: typing.List[torch.device],
        output_device: str = 'cpu'
    ):
        if (self.distributed == True):
            conf_network = DDP(
                network, 
                device_ids=device_ids, 
                output_device=output_device
            )
        else:
            device = device_ids[0]
            conf_network = network.to(device=device)
        return conf_network
    
    def configure_optimizer(self, network: nn.Module, optimizer_config: typing.Dict) -> nn.Module:

        optimizer_name = optimizer_config.get("name")
        learning_rate = optimizer_config.get("learning_rate")
        weight_decay = optimizer_config.get("weight_decay", None)
        use_nesterov = optimizer_config.get("nesterov", False)

        if optimizer_name.lower() == 'adam':
            optimizer = optim.Adam(
                params=network.parameters(),
                lr=learning_rate,
                weight_decay=weight_decay
            )

        elif optimizer_name.lower() == 'adamax':
            optimizer = optim.Adamax(
                params=network.parameters(),
                lr=learning_rate,
                weight_decay=weight_decay
            )
        
        elif optimizer_name.lower() == 'rmsprop':
            optimizer = optim.RMSprop(
                params=network.parameters(),
                lr=learning_rate,
                weight_decay=weight_decay
            )
        
        elif optimizer_name.lower() == 'sgd':
            optimizer = optim.SGD(
                params=network.parameters(),
                weight_decay=weight_decay,
                learning_rate=learning_rate,
                nesterov=use_nesterov
            )
        else:
            raise NotImplemented()

        if (self.distributed == True):
            optimizer = zero.ZeroRedundancyOptimizer(
                params=network.parameters(),
                optimizer_class=optimizer_name,
                lr=learning_rate,
                weight_decay=weight_decay,
            )
        return optimizer

    def configure_loader(self, 
        dataset: data.Dataset, 
        num_workers: int,
        batch_size: int, 
        distributed: bool = False,
        num_replicas: int = 1) -> data.DataLoader:
        if distributed:
            return data.DataLoader(
                dataset=dataset,
                batch_size=batch_size,
                pin_memory=True,
                shuffle=False,
                num_workers=num_workers,
                sampler=data.DistributedSampler(
                    dataset=dataset, 
                    num_replicas=num_replicas
                ),
            )
        else:
            return data.DataLoader(
                dataset=dataset,
                batch_size=batch_size,
                shuffle=False,
                num_workers=num_workers
            )

    def configure_lr_scheduler(self, 
        optimizer: nn.Module, 
        lr_scheduler_config: typing.Dict) -> nn.Module:
        """
        Supports:
            'poly', 'step', 'multistep', 'exp';
        """
        name = lr_scheduler_config.get("name")
        verbose = lr_scheduler_config.get("verbose", False)
        gamma = lr_scheduler_config.get("gamma")
        total_iters = lr_scheduler_config.get("total_iters")

        if name == 'poly':
            return lr_scheduler.PolynomialLR(
                optimizer=optimizer,
                total_iters=total_iters,
                power=gamma,
                verbose=verbose
            )
        if name == 'step':
            step_size = lr_scheduler_config.get("step_size")
            return lr_scheduler.StepLR(
                optimizer=optimizer,
                step_size=step_size,
                gamma=gamma,
                verbose=verbose
            )

        if name == 'multistep':
            steps = lr_scheduler_config.get("steps")
            return lr_scheduler.MultiStepLR(
                optimizer=optimizer,
                milestones=steps,
                gamma=gamma,
                verbose=verbose
            )

        if name == 'exp':
            return lr_scheduler.ExponentialLR(
                optimizer=optimizer,
                gamma=gamma,
                verbose=verbose
            )

    def configure_device(self, device_name: str):
        return device(device_name)

    def configure_callbacks(self, base_log_dir: typing.Union[str, pathlib.Path]):

        report_log_dir = os.path.join(base_log_dir, "reports")
        cpu_log_dir = os.path.join(base_log_dir, "cpu")
        gpu_log_dir = os.path.join(base_log_dir, "gpu")

        snapshot_log_dir = os.path.join(base_log_dir, "snapshots")
        snapshot_ext = self.snapshot_config.get("snapshot_ext")
        save_every = self.snapshot_config.get("save_every")

        min_diff = self.early_stopping_config.get("min_diff")
        patience = self.early_stopping_config.get("patience")
        validation_dataset = self.early_stopping_config.get("validation_dataset")
        
        self.callbacks = [
            logistics.LogisticsCallback(log_dir=report_log_dir),
            devices.CPUInferenceCallback(log_dir=cpu_log_dir),
            devices.GPUInferenceCallback(log_dir=gpu_log_dir),
            checkpoints.SnapshotCallback(
                snapshot_ext=snapshot_ext, 
                save_every=save_every, 
                log_dir=snapshot_log_dir
            ),
            early_stopping.EarlyStoppingCallback(
                min_diff=min_diff,
                patience=patience,
                validation_dataset=validation_dataset
            )
        ]
        if self.distributed:
            dist_callback = call_dist.DistributedTrainCallback(
                rank=self.dist_rank,
                backend=self.dist_backend,
                world_size=self.world_size,
                group_name=self.group_name
            )
            self.callbacks.append(dist_callback)

    def configure_seed(self, input_seed: int):
        """
        Set network behaviour to be deterministic,
        including data loading, etc.
        Warning:
            do not use this method during training,
            it's main purpose lies in ability
            to provide an option for debugging tasks
            and may dramatically slow down training speed.
        """
        torch.manual_seed(seed=input_seed)
        random.seed(a=input_seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    def configure_loader(self, dataset: dataset.Dataset):

        if not self.distributed:
            return data.DataLoader(
                dataset=dataset,
                batch_size=self.batch_size,
                num_workers=self.num_workers,
                shuffle=True,
            )
        else:
            return data.DataLoader(
                dataset=dataset,
                batch_size=self.batch_size,
                num_workers=self.num_workers,
                pin_memory=True,
            )
    
    def predict_embs(self, 
        data_sample: typing.Tuple[
            torch.Tensor, 
            torch.Tensor, 
            torch.Tensor
        ]) -> torch.Tensor:
        embs = []
        for idx, modality in enumerate(data_sample):
            pred_emb = self.networks[idx].forward(modality)
            embs.append(pred_emb)
        return embs

    def train(self, train_dataset: dataset.Dataset):

        global_step = 0
        curr_loss = float('inf')

        self.network.train()
        loader = self.configure_loader(train_dataset)
        self.on_init_end()

        for epoch in range(self.max_epochs):
            self.on_train_batch_start()

            for videos, texts, audios, labels in tqdm(
                    loader, 
                    desc='epoch: %s; curr_loss: %s;' % (
            epoch, curr_loss)):
                
                samples = list(zip(videos, texts, audios))

                # finding hard pairs of (pos_sample, sample, neg_sample) for
                # contrastive learning training, using current batch
                hard_pairs = self.contrastive_sampler.hard_mining(
                    batch_data=samples, 
                    batch_labels=labels
                )
                
                for pos_pair, pair, neg_pair in hard_pairs:

                    pos_pair_v_emb, pos_pair_t_emb = self.predict_embs(pos_pair)
                    pair_v_emb, pair_t_emb  = self.predict_embs(pair)
                    neg_pair_v_emb, neg_pair_t_emb = self.predict_embs(neg_pair)
                    
                    img_loss = self.pair_loss_function(pos_pair_v_emb, pair_v_emb, neg_pair_v_emb)
                    text_loss = self.pair_loss_function(pos_pair_t_emb, pair_t_emb, neg_pair_t_emb)
                    modal_sim_loss = self.modal_loss_function(pair_v_emb, pair_t_emb)
                    
                    # overall loss function: summary of image similarity pairs, text similarity pairs
                    # and similarity between modalities

                    overall_loss = img_loss.item() + text_loss.item() + modal_sim_loss.item()
                    
                    # in case we are using single gpu, we traverse
                    # over all computed loss (for each modality) and after each update
                    # clear gradients 

                    overall_loss.backward()

                    for idx in range(len(self.optimizers)):

                        self.optimizers[idx].step()

                        if len(self.lr_schedulers) > 0:
                            self.lr_schedulers[idx].step()

                        # emptying the gradients, so they does not overlap
                        # with next ones, when training multiple networks
                        # on the same device.
                        self.optimizers[idx].zero_grad()

            # we pass argument 'trainer' to this event
            # in case early stopping callback want to say us, that training is done.
            # It will update flag 'stop' to True
            self.on_train_batch_end(trainer=self)
            
            # global step is simply used to track current epoch.
            self.on_validation_start(global_step=global_step)
            self.on_validation_end(global_step=global_step)
            self.on_train_epoch_end(global_step=global_step)

            if self.stop: break
        self.tearDown()

        return curr_loss

    def evaluate(self, validation_dataset: dataset.Dataset):
        
        with torch.no_grad():
            loader = self.configure_loader(validation_dataset)

            video_metrics = []
            text_metrics = []
            audio_metrics = []

            for videos, texts, audios, _ in loader:

                sample = list(zip(videos, texts, audios))
                hard_pairs = self.contrastive_sampler.hard_mining(sample)
                
                for pos_sample, sample, neg_sample in hard_pairs:

                    pos_emb = self.predict_embs(pos_sample)
                    pred_emb = self.predict_embs(sample)
                    neg_emb = self.predict_embs(neg_sample)
                    
                    video_metric = self.eval_metric(
                        pred_emb[0], pos_emb[0], neg_emb[0])

                    audio_metric = self.eval_metric(
                        pred_emb[1], pos_emb[1], neg_emb[1])

                    text_metric = self.eval_metric(
                        pred_emb[2], pos_emb[2], neg_emb[2]
                    )
                video_metrics.append(video_metric)
                text_metrics.append(text_metrics)
                audio_metrics.append(audio_metrics)
    
            return (
                numpy.mean(video_metrics), 
                numpy.mean(text_metrics), 
                numpy.mean(audio_metrics)
            ) 

    def sliced_evaluate(self, embeddings: typing.List[torch.Tensor], labels: typing.List):
        """
        Evaluates embeddings on individual slices of data,
        based on the label.
        """
        output_metrics: typing.Dict[str, float] = {}
        unique_labels = numpy.unique(labels)
        for label in unique_labels:
            indices = numpy.where(labels == label)[0]
            cat_embeddings = [emb for emb in embeddings if emb in indices]
            metric = self.find_similarity(cat_embeddings)
            output_metrics[label] = metric
        return output_metrics



