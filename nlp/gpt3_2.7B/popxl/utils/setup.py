# Copyright (c) 2022 Graphcore Ltd. All rights reserved.

import argparse
import atexit
import logging
import os
import random
import tempfile
from argparse import ArgumentParser
from pathlib import Path
from typing import Optional, Callable, Tuple, Union, List, Literal, Dict

import numpy as np
from transformers import GPT2Model as HFGPT2Model, GPT2Config as HFGPT2Config, GPT2LMHeadModel as HFGPT2LMHeadModel
import popart
import torch
import wandb
import popdist

from config import GPTConfig
from popxl_addons import GIT_COMMIT as ADDONS_GIT_COMMIT, timer
from utils.simple_parsing_tools import parse_args_with_presets


def dict_startswith(d: Dict, key_startswith: str) -> str:
    keys = sorted(d.keys())  # Make deterministic if duplicate matching keys
    try:
        key = next(k for k in keys if key_startswith.startswith(k))  # Return first match
        return d[key]
    except StopIteration as e:
        return None


def gpt_config_setup(
    config_file: Union[str, Path],
    presets_key: str,
    default: str,
    wandb_setup: bool = False,
    hf_model_setup: bool = False,
    CLI_args: Optional[str] = None,
    custom_args=None,
) -> Tuple[GPTConfig, argparse.Namespace, Optional[HFGPT2Model]]:
    """Parse command line args, setup random seed, W&B, logging and
    load a pre-trained model.

    Args:
        config_file: Path to config file (yaml)
        presets_key: Which key in the config to use
        default: Default model config
        wandb_setup: Should it initialise Weights and Biases
        hf_model_setup: Should it add arguments to load an HF pretrained model and load the model if the user
            specifies
        CLI_args: Additional CLI args used for debugging

    Returns:
        GPTConfig, argparse namespace and optional pretrained model
    """
    config_to_hf = {  # layers
        "gpt2_small": "gpt2",  # 12
        "gpt2_medium": "gpt2-medium",  # 24
        "gpt2_large": "gpt2-large",  # 36
        "gpt2_xl": "gpt2-xl",  # 48
        "cerebras_gpt_111M": "cerebras/Cerebras-GPT-111M",  # 10
        "cerebras_gpt_1_3B": "cerebras/Cerebras-GPT-1.3B",  # 24
        "cerebras_gpt_6_7B": "cerebras/Cerebras-GPT-6.7B",  # 32
        "cerebras_gpt_13B": "cerebras/Cerebras-GPT-13B",  # 40
    }

    def custom_args_(parser: ArgumentParser):
        if custom_args:
            custom_args(parser)

        log_level = os.environ.get("APP_LOG_LEVEL", "INFO")
        parser.add_argument(
            "--log_level",
            choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            type=str,
            default=log_level,
            help=("Logging level for the app. " "Can also be set using the environment variable `APP_LOG_LEVEL`"),
        )

        if hf_model_setup:
            parser.add_argument(
                "--hf_model",
                type=str,
                help="HuggingFace transformers pre-trained model to load. "
                "Use 'None' to deliberately skip loading a model for debugging. "
                "Use 'Test' to create a garbage HF model to load. "
                "If no value is provided it will automatically try and match to the config.",
            )

        if wandb_setup:
            parser.add_argument(
                "--wandb", default="False", choices=["False", "True"], help="Initialise Weights and Biases"
            )

    config, args = parse_args_with_presets(GPTConfig, config_file, presets_key, default, custom_args_, CLI_args)
    config: GPTConfig  # type: ignore
    config.validate()

    np.random.seed(config.model.seed)
    torch.manual_seed(config.model.seed)
    random.seed(config.model.seed)

    if wandb_setup:
        wandb_init(config, tags=["PE", "TP"], disable=args.wandb == "False")

    logging_setup(args, config)

    # Loads a HuggingFace Language Model. Need to add switch if require other types
    if hf_model_setup:
        if args.hf_model == "None":
            pretrained = None
        elif args.hf_model == "Test":
            hf_config = HFGPT2Config(
                vocab_size=config.model.embedding.vocab_size,
                n_positions=config.model.sequence_length,
                n_embd=config.model.hidden_size,
                n_layer=config.model.layers,
                n_head=config.model.attention.heads,
                resid_pdrop=config.model.dropout_prob,
                embd_pdrop=config.model.dropout_prob,
                attn_pdrop=config.model.dropout_prob,
            )
            pretrained = HFGPT2LMHeadModel(hf_config)
        else:
            if args.hf_model is not None:
                hf_model = args.hf_model
            elif dict_startswith(config_to_hf, args.config):
                hf_model = dict_startswith(config_to_hf, args.config)
            else:
                raise ValueError(
                    "Could not match config with `hf_model` automatically. Please provide a hugging face model name or `None`"
                )
            with timer("Loading HF model to host"):
                pretrained = HFGPT2LMHeadModel.from_pretrained(hf_model)
            xl_hf_config_check(config, pretrained.config)
    else:
        pretrained = None

    return config, args, pretrained


def gpt_training_setup(
    config_file: Union[str, Path],
    presets_key: str,
    default_config: str,
) -> Tuple[GPTConfig, argparse.Namespace, Optional[HFGPT2Model]]:
    """GPT setup for pre-training scripts"""
    config, args, pretrained = gpt_config_setup(
        config_file, presets_key, default_config, wandb_setup=True, hf_model_setup=True
    )

    return config, args, pretrained


def logging_setup(args, config):
    """Setup logging"""
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S", force=True
    )
    logging.info(f"Staring. Process id: {os.getpid()}")
    logging.info(f"Config: {config}")
    logging.info(f"Gradient accumulation steps: {config.gradient_accumulation}")


def xl_hf_config_check(config: GPTConfig, hf_config: HFGPT2Config):
    """Compare a GPTConfig with a Hugging Face config and ensure they match. Required if loading a pre-trained model"""
    params = [
        ("hidden_size", config.model.hidden_size, hf_config.n_embd),
        ("heads", config.model.attention.heads, hf_config.n_head),
        ("layers", config.model.layers, hf_config.n_layer),
    ]
    if not all(xl == hf for _, xl, hf in params):
        not_eq_str = ", ".join(f"\n`{name}` not equal, config: {xl}, hf: {hf}" for name, xl, hf in params if xl != hf)
        raise ValueError(f"Config does not match the Hugging Face (hf) pre-trained model. Not matching: {not_eq_str}")


def wandb_init(config: GPTConfig, tags: Optional[List[str]] = None, disable: bool = False):
    """Setup weights and biases"""

    # Save config with addons and popxl version
    config_dict = config.to_dict()
    config_dict["gradient_accumulation"] = config.gradient_accumulation
    config_dict["ipus"] = config.ipus
    config_dict["addons_version"] = ADDONS_GIT_COMMIT
    config_dict["popxl_version"] = popart.versionString()

    mode = "disabled" if disable else "online"
    if popdist.getInstanceIndex() != 0:
        mode = "disabled"

    wandb.init(project="popxl-gpt", tags=tags, config=config_dict, mode=mode)

    # Upload config yml
    # Wandb uploads file asynchronously so can't use a normal context manager
    tmp_dir_cm = tempfile.TemporaryDirectory()
    tmp_dir = tmp_dir_cm.__enter__()
    atexit.register(lambda: tmp_dir_cm.__exit__(None, None, None))  # Delete directory on exit
    tmp_path = os.path.join(tmp_dir, "config.yml")
    with open(tmp_path, "w") as f:
        config.dump_yaml(f)
    wandb.save(tmp_path, base_path=tmp_dir, policy="now")
