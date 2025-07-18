from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import torch
import wandb
import gc
from lightning import Trainer, seed_everything
from lightning.pytorch.callbacks import ModelCheckpoint

from utils.general import get_data_paths, parser_command_line
from initializers.initialize_dataloader import initialize_dataloader
from initializers.initialize_model import initialize_model
from initializers.initialize_parameters import initialize_parameters, initialize_ckpt_args, initialize_wandb_logger


def run():
    torch.backends.cudnn.enabled = True
    torch.set_float32_matmul_precision("medium")
    
    # Initialize and override args, parameters, and paths
    args = parser_command_line() # Load the arguments from the command line
    paths = get_data_paths() # Get the file path from the .env file
    params = initialize_parameters(args)
    seed_everything(params.general.seed, workers=True) # Sets seeds for numpy, torch and python.random.

    # Initialize wandb logging
    logger, wandb_run_name, time_now = initialize_wandb_logger(args, paths, params)

    # Initialize data module
    data_module = initialize_dataloader(args, params, paths)

    # Initialze lighting module
    model = initialize_model(args, params, paths, data_module)
    
    # Check the resuming and loading of the checkpoints
    if params.general.resume_training:  # Resume training
        assert params.general.resume_ckpt_path != None, "The path for checkpoint is not provided."
        resume_ckpt_path = Path(paths.log_folder) / params.general.resume_ckpt_path
        ckpt_dir = resume_ckpt_path.parent
        if wandb_run_name != ckpt_dir.parent.name and wandb_run_name is not None:
            ckpt_dir = resume_ckpt_path.parent.parent.parent / wandb_run_name / time_now
        print(f"ckpt_dir: {ckpt_dir}")
        checkpoint = torch.load(resume_ckpt_path, weights_only=False, map_location="cpu")
        model.load_state_dict(checkpoint["state_dict"], strict=True)
    else:
        # resume_ckpt_path = None
        ckpt_dir = os.path.join(f"{paths.log_folder}/checkpoints_{args.module}/{wandb_run_name}/{time_now}")
    
    # Monitor foreground dice for segmentation. When reconstruction, monitor PSNR. MAE for regression.
    monitor_metric, ckpt_filename, monitor_mode = initialize_ckpt_args(args, params)
            
    os.makedirs(ckpt_dir, exist_ok=True)
    checkpoint_callback = ModelCheckpoint(dirpath=ckpt_dir, filename=ckpt_filename, monitor=monitor_metric, 
                                          mode=monitor_mode, save_top_k=1, save_last=True, verbose=True,
                                          every_n_epochs=params.module.training_hparams.val_log_rate, )
    
    # Initialize trainer
    trainer = Trainer(
        default_root_dir=paths.log_folder,
        logger=logger,
        callbacks=[checkpoint_callback,],
        fast_dev_run=False,
        limit_train_batches=1.0,
        limit_val_batches=1.0,
        num_sanity_val_steps=2,
        benchmark=True,
        profiler="simple",
        strategy="ddp" if params.trainer.devices > 1 else "auto",
        
        **params.trainer.__dict__,
    )

    if args.pipeline == "train":
        model.save_embeddings = False
        trainer.fit(model, datamodule=data_module)
    elif args.pipeline == "val":
        trainer.validate(model, datamodule=data_module)
    elif args.pipeline == "test":
        trainer.test(model, datamodule=data_module)
    else:
        raise ValueError
        
    # Clean up WandB and free up memory
    wandb.finish() 
    del model, data_module, trainer, logger
    gc.collect()
    torch.cuda.empty_cache()


if __name__ == "__main__":
    os.environ["CUDA_LAUNCH_BLOCKING"] = "1"
    run()
