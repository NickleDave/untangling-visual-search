import numpy as np
import torch
import torch.nn as nn

from .engine.transfer_trainer import TransferTrainer
from .engine.trainer import Trainer
from .datasets import Searchstims, VOCDetection
from .utils.general import make_save_path
from .transforms.functional import VSD_PAD_SIZE
from .transforms.util import get_transforms


def train(csv_file,
          dataset_type,
          net_name,
          number_nets_to_train,
          epochs_list,
          batch_size,
          random_seed,
          save_path,
          root=None,
          pad_size=VSD_PAD_SIZE,
          method='transfer',
          num_classes=2,
          learning_rate=None,
          base_learning_rate=1e-20,
          new_learn_rate_layers='fc8',
          new_layer_learning_rate=0.001,
          freeze_trained_weights=True,
          loss_func='CE',
          optimizer='SGD',
          use_val=True,
          val_epoch=1,
          summary_step=None,
          patience=None,
          checkpoint_epoch=10,
          save_acc_by_set_size_by_epoch=False,
          num_workers=4,
          data_parallel=False):
    """train neural networks to perform visual search task.

    Parameters
    ----------
    csv_file : str
        name of .csv file containing prepared data sets.
        Generated by searchnets.data.split function from a csv created by the searchstims library.
    dataset_type : str
        one of {'searchstims', 'VSD'}. Specifies whether dataset is images generated by searchstims package, or
        images from Pascal-VOC dataset that were used to create the Visual Search Difficulty 1.0 dataset.
    net_name : str
        name of neural net architecture to train.
        One of {'alexnet', 'VGG16', 'CORnet_Z', 'CORnet_RT', 'CORnet_S'}
    number_nets_to_train : int
        number of training "replicates"
    epochs_list : list
        of training epochs. Replicates will be trained for each
        value in this list. Can also just be one value, but a list
        is useful if you want to test whether effects depend on
        number of training epochs.
    batch_size : int
        number of samples in a batch of training data
    random_seed : int
        to seed random number generator
    save_path : str
        path to directory where checkpoints and train models were saved
    root : str
        path to dataset root. Used with VOCDetection dataset to specify where VOC data was/should be downloaded
        to. (Note that download will take a while.)
    method : str
        training method. One of {'initialize', 'transfer'}.
        'initialize' means randomly initialize all weights and train the
        networks "from scratch".
        'transfer' means perform transfer learning, using weights pre-trained
        on imagenet.
        Default is 'transfer'.

    Other Parameters
    ----------------
    num_classes : int
        number of classes. Default is 2 (target present, target absent).
    base_learning_rate : float
        Applied to layers with weights loaded from training the
        architecture on ImageNet. Should be a very small number
        so the trained weights don't change much.
    freeze_trained_weights : bool
        if True, freeze weights in any layer not in "new_learn_rate_layers".
        These are the layers that have weights pre-trained on ImageNet.
        Default is False. Done by simply not applying gradients to these weights,
        i.e. this will ignore a base_learning_rate if you set it to something besides zero.
    new_learn_rate_layers : list
        of layer names whose weights will be initialized randomly
        and then trained with the 'new_layer_learning_rate'.
    new_layer_learning_rate : float
        Applied to `new_learn_rate_layers'. Should be larger than
        `base_learning_rate` but still smaller than the usual
        learning rate for a deep net trained with SGD,
        e.g. 0.001 instead of 0.01
    loss_func : str
        type of loss function to use. One of {'CE', 'BCE'}.
        Default is 'CE', the standard cross-entropy loss.
    optimizer : str
        optimizer to use. One of {'SGD', 'Adam', 'AdamW'}.
    pad_size : int
        size to which images in PascalVOC / Visual Search Difficulty dataset should be padded.
        Images are padded by making an array of zeros and randomly placing the image within it
        so that the entire image is still within the boundaries of (pad size x pad size).
        Default value is specified by searchnets.transforms.functional.VSD_PAD_SIZE.
        Argument has no effect if the dataset_type is not 'VOC'.
    save_acc_by_set_size_by_epoch : bool
        if True, compute accuracy on training set for each epoch separately
        for each unique set size in the visual search stimuli. These values
        are saved in a matrix where rows are epochs and columns are set sizes.
        Useful for seeing whether accuracy converges for each individual
        set size. Default is False.
    use_val : bool
        if True, use validation set.
    val_epoch : int
        if not None, accuracy on validation set will be measured every `val_epoch` epochs. Default is None.
    summary_step : int
        Step on which to write summaries to file. Each minibatch is counted as one step, and steps are counted across
        epochs. Default is None.
    patience : int
        if not None, training will stop if accuracy on validation set has not improved in `patience` steps
    num_workers : int
        number of workers used by torch.DataLoaders. Default is 4.
    data_parallel : bool
        if True, use torch.nn.dataparallel to train network on multiple GPUs. Default is False.

    Returns
    -------
    None
    """
    if use_val and val_epoch is None or val_epoch < 1 or type(val_epoch) != int:
        raise ValueError(
            f'invalid value for val_epoch: {val_epoch}. Validation epoch must be positive integer'
        )

    if use_val is False and patience is not None:
        raise ValueError('patience argument only works with a validation set')

    if patience is not None:
        if type(val_epoch) != int or patience < 1:
            raise TypeError('patience must be a positive integer')

    if type(epochs_list) is int:
        epochs_list = [epochs_list]
    elif type(epochs_list) is list:
        pass
    else:
        raise TypeError("'EPOCHS' option in 'TRAIN' section of config.ini file parsed "
                        f"as invalid type: {type(epochs_list)}")

    if random_seed:
        np.random.seed(random_seed)  # for shuffling in batch_generator
        torch.manual_seed(random_seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    if torch.cuda.is_available():
        device = torch.device('cuda')
    else:
        device = torch.device('cpu')

    transform, target_transform = get_transforms(dataset_type, loss_func, pad_size)

    if dataset_type == 'VSD':
        trainset = VOCDetection(root=root,
                                csv_file=csv_file,
                                image_set='trainval',
                                split='train',
                                download=True,
                                transform=transform,
                                target_transform=target_transform
                                )
        if use_val:
            valset = VOCDetection(root=root,
                                  csv_file=csv_file,
                                  image_set='trainval',
                                  split='val',
                                  download=True,
                                  transform=transform,
                                  target_transform=target_transform
                                  )
        else:
            valset = None

    elif dataset_type == 'searchstims':
        trainset = Searchstims(csv_file=csv_file,
                               split='train',
                               transform=transform)

        if use_val:
            valset = Searchstims(csv_file=csv_file,
                                 split='val',
                                 transform=transform)
        else:
            valset = None

    if save_acc_by_set_size_by_epoch:
        if dataset_type == 'VSD':
            raise ValueError(
                'dataset type is VSD but save_acc_by_set_size_by_epoch was set to True;'
                'can only measure accuracy by set size with searchstims stimuli, not VSD dataset'
            )
        elif dataset_type == 'searchstims':
            trainset_set_size = Searchstims(csv_file=csv_file,
                                            split='train',
                                            transform=transform,
                                            return_set_size=True)
    else:
        trainset_set_size = None

    if loss_func in {'CE', 'CE-largest', 'CE-random'}:
        apply_sigmoid = False
        criterion = nn.CrossEntropyLoss()
    elif loss_func == 'BCE':
        apply_sigmoid = True  # for multi-label prediction
        criterion = nn.BCELoss()
    else:
        raise ValueError(
            f'invalid value for loss function: {loss_func}'
        )

    for epochs in epochs_list:
        print(f'training {net_name} model for {epochs} epochs')
        for net_number in range(1, number_nets_to_train + 1):
            save_path_this_net = make_save_path(save_path, net_name, net_number, epochs)
            if method == 'transfer':
                trainer = TransferTrainer.from_config(net_name=net_name,
                                                      trainset=trainset,
                                                      new_learn_rate_layers=new_learn_rate_layers,
                                                      freeze_trained_weights=freeze_trained_weights,
                                                      base_learning_rate=base_learning_rate,
                                                      new_layer_learning_rate=new_layer_learning_rate,
                                                      save_path=save_path_this_net,
                                                      num_classes=num_classes,
                                                      apply_sigmoid=apply_sigmoid,
                                                      criterion=criterion,
                                                      optimizer=optimizer,
                                                      save_acc_by_set_size_by_epoch=save_acc_by_set_size_by_epoch,
                                                      trainset_set_size=trainset_set_size,
                                                      batch_size=batch_size,
                                                      epochs=epochs,
                                                      use_val=use_val,
                                                      valset=valset,
                                                      val_epoch=val_epoch,
                                                      patience=patience,
                                                      checkpoint_epoch=checkpoint_epoch,
                                                      summary_step=summary_step,
                                                      device=device,
                                                      num_workers=num_workers,
                                                      data_parallel=data_parallel)
            elif method == 'initialize':
                trainer = Trainer.from_config(net_name=net_name,
                                              trainset=trainset,
                                              save_path=save_path_this_net,
                                              num_classes=num_classes,
                                              apply_sigmoid=apply_sigmoid,
                                              criterion=criterion,
                                              optimizer=optimizer,
                                              learning_rate=learning_rate,
                                              save_acc_by_set_size_by_epoch=save_acc_by_set_size_by_epoch,
                                              trainset_set_size=trainset_set_size,
                                              batch_size=batch_size,
                                              epochs=epochs,
                                              use_val=use_val,
                                              valset=valset,
                                              val_epoch=val_epoch,
                                              patience=patience,
                                              checkpoint_epoch=checkpoint_epoch,
                                              summary_step=summary_step,
                                              device=device,
                                              num_workers=num_workers,
                                              data_parallel=data_parallel)

            trainer.train()
