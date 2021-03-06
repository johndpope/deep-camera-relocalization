from __future__ import print_function

import numpy as np
import argparse, os, importlib

from sklearn.model_selection import train_test_split
from keras.models import load_model
from keras.callbacks import (
  LearningRateScheduler, 
  ModelCheckpoint,
  EarlyStopping,
  ReduceLROnPlateau
)

from models import pose_model, losses, metrics
from utils import (
  make_dir, 
  load_labels, 
  ExtendedLogger,
  ResetStatesCallback,
  get_starting_indicies)
import cnn

import keras.backend as K

def create_callbacks(output_dir='/tmp', prediction_layer=None, 
  run_identifier=None, save_period=1, starting_indicies=None,
  stateful=False, stateful_reset_interval=None, **hyperparams):
  '''
  Custom logger runs prediction at the end of a training epoch 
  for the validation dataset. In order to retrieve the prediction 
  on a specific layer, we have to pass the names of these layers. 
  This allows for custom loss functions with trainable parameters.
  @see .losses
  '''
  early_stopper = EarlyStopping(monitor='val_loss', min_delta=0.0, 
    patience=7, verbose=1)

  output_dir = os.path.join(output_dir, run_identifier)

  logger = ExtendedLogger(prediction_layer, 
    output_dir=output_dir,
    starting_indicies=starting_indicies, 
    stateful=stateful, 
    stateful_reset_interval=stateful_reset_interval)

  logger.add_validation_metrics(metrics.PoseMetrics.get_all_metrics())
  
  mc_directory = os.path.join(output_dir, 'checkpoints')
  make_dir(mc_directory)
  checkpoint_pattern = 'weights.{epoch:04d}-{val_loss:.4f}.hdf5'
  model_checkpoint = ModelCheckpoint(
    os.path.join(mc_directory, checkpoint_pattern),
    save_weights_only=False,
    save_best_only=False,
    period=save_period
  )

  return [logger, model_checkpoint, early_stopper]

def hyperparam_search(model_class, X_train, y_train, X_val, y_val,
  config=None, output_dir=None, iters=50, save_period=1, epochs=1000,
  batch_size=128, stateful=False, stateful_reset_interval=None, starting_indicies=None):

  if not config:
    raise ValueError('Hyperparam config has to be specified!')

  if not output_dir:
    raise ValueError('Output has to be specified!')

  for _ in xrange(iters):

    hyperparameter_space = config.space
    hyperparams = {var: gen() for var, gen in hyperparameter_space.items()}
    hyperparam_desc = config.desc.format(**hyperparams)

    callbacks = create_callbacks(output_dir=output_dir, 
      prediction_layer='prediction', 
      run_identifier=hyperparam_desc,
      save_period=save_period,
      stateful=stateful,
      stateful_reset_interval=stateful_reset_interval,
      starting_indicies=starting_indicies
    )

    if stateful:
      callbacks.append(ResetStatesCallback(interval=stateful_reset_interval))

    lr_modifier = hyperparams['lr_modifier']
    lr_modifier_callback = lr_modifier(**hyperparams)
    if lr_modifier_callback is not None:
      callbacks.append(lr_modifier_callback)

    model = model_class(**hyperparams).build()
    model.summary()

    print('Hyper-parameters:')
    print(hyperparams)
    print('Run-identifier:')
    print(hyperparam_desc)

    model.fit(X_train, y_train, 
      batch_size=batch_size,
      validation_data=(X_val, y_val),
      epochs=epochs,
      callbacks=callbacks,
      verbose=True,
      shuffle=(not stateful)
    )

def main():
  parser = argparse.ArgumentParser()
  
  parser.add_argument('-tl', '--train-labels', nargs='+', required=True, 
    help='Path to a directory with training labels')
  parser.add_argument('-tf', '--train-features', nargs='+', required=True,
    help='Path to a numpy array with training features')

  parser.add_argument('-vl', '--val-labels', nargs='+', required=True, 
    help='Path to a directory with validation labels')
  parser.add_argument('-vf', '--val-features', nargs='+', required=True,
    help='Path to a numpy array with validation features')

  parser.add_argument('-o', '--output-dir', required=True, 
    help='Path to an output dir with tensorboard logs, csv, checkpoints, etc')
 
  parser.add_argument('-m', '--mode', default='initial',
    choices=pose_model.PoseModel.MODES,
    help='Training mode, initial or finetuning')

  parser.add_argument('--model-weights',
    help='Model\'s weights to be loaded finetuning or prediction')
  parser.add_argument('--finetuning-model-arch', choices=cnn.CNNS,
    help='Model architecture for finetuning')
  parser.add_argument('--finetuning-model-dataset', choices=cnn.DATASETS,
    help='Dataset on which finetuning model was pretrained ')

  parser.add_argument('-tm', '--top-model-type', default='regressor',
    choices=pose_model.PoseModel.TOPMODELS.keys(),
    help='Top model to use for regression')
  parser.add_argument('--seq-len', type=int,
    help='If top-model-type is an LSTM, then seq-len has to be specified!')

  parser.add_argument('--subseq-len', type=int,
    help='If top-model-type is a stateful LSTM, then subseq-len'
      'has to be specified!')

  parser.add_argument('--loss', default='naive-weighted',
    choices=losses.LOSSES.keys(),
    help='Loss function to use for optimization')
  parser.add_argument('-hp', '--hyperparam-config', required=True,
    help='Python file with hyperparameter configuration')

  parser.add_argument('-i', '--iters', type=int, default=50,
    help='Number of iterations for the random hyperparameter search')
  parser.add_argument('-e', '--epochs', type=int, default=1000,
    help='Number of epochs per iteration')
  parser.add_argument('-bs', '--batch-size', type=int, default=128,
    help='Batch size')
  parser.add_argument('-s', '--save-period', type=int, default=1,
    help='Number of epochs to save a checkpoint')

  #parser.add_argument('-c', '--checkpoint', type=str)
  args = parser.parse_args()

  hyperparam_config = importlib.import_module(args.hyperparam_config)
  
  starting_indicies = None

  if ('stateful' in args.top_model_type) or ('standard' in args.top_model_type):
  
    train_features = np.load(args.train_features[0], mmap_mode='r')
    train_labels = np.load(args.train_labels[0], mmap_mode='r')

    val_features = np.load(args.val_features[0], mmap_mode='r')
    val_labels = np.load(args.val_labels[0], mmap_mode='r') 

  elif args.top_model_type in ['regressor', 'spatial-lstm']:
    train_features_arr = [np.squeeze(np.load(f, mmap_mode='r')) for f in args.train_features]
    train_labels_arr   = [load_labels(l) for l in args.train_labels]

    val_features_arr = [np.squeeze(np.load(f, mmap_mode='r')) for f in args.val_features]
    val_labels_arr   = [load_labels(l) for l in args.val_labels]

    valid_start_indicies = get_starting_indicies(val_features_arr)
    starting_indicies = {'valid' : valid_start_indicies}

    train_features = np.concatenate(train_features_arr)
    train_labels = np.concatenate(train_labels_arr)
    val_features = np.concatenate(val_features_arr)
    val_labels   = np.concatenate(val_labels_arr)

  print(train_features.shape, train_labels.shape)
  print(val_features.shape, val_labels.shape)

  if args.mode == 'initial':
    input_shape = (train_features.shape[-1],)
    def model_class(**hyperparams):
      return pose_model.PoseModel(
        input_shape=input_shape,
        top_model_type=args.top_model_type,
        model_loss=args.loss,
        mode=args.mode,
        batch_size=args.batch_size,
        seq_len=args.seq_len,
        subseq_len=args.subseq_len,
        **hyperparams)

  elif args.mode == 'finetune':
    def model_class(**hyperparams):
      return pose_model.PoseModel(
        input_shape=None,
        top_model_type=args.top_model_type,
        model_loss=args.loss,
        mode=args.mode,
        finetuning_model_arch=args.finetuning_model_arch,
        finetuning_model_dataset=args.finetuning_model_dataset,
        model_weights=args.model_weights,
        batch_size=args.batch_size,
        seq_len=args.seq_len,
        subseq_len=args.subseq_len,
        **hyperparams)

  if 'homoscedastic' in args.loss:
    '''
    Actual homescedastic loss is implemented in the last layer
    as it requires trainable parameters. Therefore, labels are fed with
    dummy data and secondary input is designated for the actual labels.
    Rerouting of the data happens here.
    '''
    train_features = {'main_input' : train_features, 'labels_input' : train_labels}
    train_labels = np.zeros((train_labels.shape[0],))

    val_features = {'main_input' : val_features, 'labels_input' : val_labels}
    val_labels = np.zeros((val_labels.shape[0],))

  stateful = False
  stateful_reset_interval = -1
  if 'stateful' in args.top_model_type:
    stateful = True
    stateful_reset_interval = args.seq_len // args.subseq_len
    print('Stateful reset interval is:', stateful_reset_interval)

  hyperparam_search(model_class, train_features, train_labels,
    val_features, val_labels,
    config=hyperparam_config,
    output_dir=args.output_dir,
    iters=args.iters,
    epochs=args.epochs,
    save_period=args.save_period,
    batch_size=args.batch_size,
    stateful=stateful,
    stateful_reset_interval=stateful_reset_interval,
    starting_indicies=starting_indicies)

if __name__ == '__main__':
  main()
