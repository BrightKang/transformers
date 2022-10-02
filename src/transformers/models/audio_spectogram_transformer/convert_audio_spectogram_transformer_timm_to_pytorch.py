# coding=utf-8
# Copyright 2022 The HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Convert AudioSpectogramTransformer and non-distilled DeiT checkpoints from the timm library."""


import argparse
import json
from pathlib import Path

import torch
from PIL import Image

import requests
import timm
from huggingface_hub import hf_hub_download
from transformers import DeiTFeatureExtractor, AudioSpectogramTransformerConfig, AudioSpectogramTransformerFeatureExtractor, AudioSpectogramTransformerForImageClassification, AudioSpectogramTransformerModel
from transformers.utils import logging


logging.set_verbosity_info()
logger = logging.get_logger(__name__)


# here we list all keys to be renamed (original name on the left, our name on the right)
def create_rename_keys(config, base_model=False):
    rename_keys = []
    for i in range(config.num_hidden_layers):
        # encoder layers: output projection, 2 feedforward neural networks and 2 layernorms
        rename_keys.append((f"blocks.{i}.norm1.weight", f"audio_spectogram_transformer.encoder.layer.{i}.layernorm_before.weight"))
        rename_keys.append((f"blocks.{i}.norm1.bias", f"audio_spectogram_transformer.encoder.layer.{i}.layernorm_before.bias"))
        rename_keys.append((f"blocks.{i}.attn.proj.weight", f"audio_spectogram_transformer.encoder.layer.{i}.attention.output.dense.weight"))
        rename_keys.append((f"blocks.{i}.attn.proj.bias", f"audio_spectogram_transformer.encoder.layer.{i}.attention.output.dense.bias"))
        rename_keys.append((f"blocks.{i}.norm2.weight", f"audio_spectogram_transformer.encoder.layer.{i}.layernorm_after.weight"))
        rename_keys.append((f"blocks.{i}.norm2.bias", f"audio_spectogram_transformer.encoder.layer.{i}.layernorm_after.bias"))
        rename_keys.append((f"blocks.{i}.mlp.fc1.weight", f"audio_spectogram_transformer.encoder.layer.{i}.intermediate.dense.weight"))
        rename_keys.append((f"blocks.{i}.mlp.fc1.bias", f"audio_spectogram_transformer.encoder.layer.{i}.intermediate.dense.bias"))
        rename_keys.append((f"blocks.{i}.mlp.fc2.weight", f"audio_spectogram_transformer.encoder.layer.{i}.output.dense.weight"))
        rename_keys.append((f"blocks.{i}.mlp.fc2.bias", f"audio_spectogram_transformer.encoder.layer.{i}.output.dense.bias"))

    # projection layer + position embeddings
    rename_keys.extend(
        [
            ("cls_token", "audio_spectogram_transformer.embeddings.cls_token"),
            ("patch_embed.proj.weight", "audio_spectogram_transformer.embeddings.patch_embeddings.projection.weight"),
            ("patch_embed.proj.bias", "audio_spectogram_transformer.embeddings.patch_embeddings.projection.bias"),
            ("pos_embed", "audio_spectogram_transformer.embeddings.position_embeddings"),
        ]
    )

    if base_model:
        # layernorm + pooler
        rename_keys.extend(
            [
                ("norm.weight", "layernorm.weight"),
                ("norm.bias", "layernorm.bias"),
                ("pre_logits.fc.weight", "pooler.dense.weight"),
                ("pre_logits.fc.bias", "pooler.dense.bias"),
            ]
        )

        # if just the base model, we should remove "audio_spectogram_transformer" from all keys that start with "audio_spectogram_transformer"
        rename_keys = [(pair[0], pair[1][4:]) if pair[1].startswith("audio_spectogram_transformer") else pair for pair in rename_keys]
    else:
        # layernorm + classification head
        rename_keys.extend(
            [
                ("norm.weight", "audio_spectogram_transformer.layernorm.weight"),
                ("norm.bias", "audio_spectogram_transformer.layernorm.bias"),
                ("head.weight", "classifier.weight"),
                ("head.bias", "classifier.bias"),
            ]
        )

    return rename_keys


# we split up the matrix of each encoder layer into queries, keys and values
def read_in_q_k_v(state_dict, config, base_model=False):
    for i in range(config.num_hidden_layers):
        if base_model:
            prefix = ""
        else:
            prefix = "audio_spectogram_transformer."
        # read in weights + bias of input projection layer (in timm, this is a single matrix + bias)
        in_proj_weight = state_dict.pop(f"blocks.{i}.attn.qkv.weight")
        in_proj_bias = state_dict.pop(f"blocks.{i}.attn.qkv.bias")
        # next, add query, keys and values (in that order) to the state dict
        state_dict[f"{prefix}encoder.layer.{i}.attention.attention.query.weight"] = in_proj_weight[
            : config.hidden_size, :
        ]
        state_dict[f"{prefix}encoder.layer.{i}.attention.attention.query.bias"] = in_proj_bias[: config.hidden_size]
        state_dict[f"{prefix}encoder.layer.{i}.attention.attention.key.weight"] = in_proj_weight[
            config.hidden_size : config.hidden_size * 2, :
        ]
        state_dict[f"{prefix}encoder.layer.{i}.attention.attention.key.bias"] = in_proj_bias[
            config.hidden_size : config.hidden_size * 2
        ]
        state_dict[f"{prefix}encoder.layer.{i}.attention.attention.value.weight"] = in_proj_weight[
            -config.hidden_size :, :
        ]
        state_dict[f"{prefix}encoder.layer.{i}.attention.attention.value.bias"] = in_proj_bias[-config.hidden_size :]


def remove_classification_head_(state_dict):
    ignore_keys = ["head.weight", "head.bias"]
    for k in ignore_keys:
        state_dict.pop(k, None)


def rename_key(dct, old, new):
    val = dct.pop(old)
    dct[new] = val


# We will verify our results on an image of cute cats
def prepare_img():
    url = "http://images.cocodataset.org/val2017/000000039769.jpg"
    im = Image.open(requests.get(url, stream=True).raw)
    return im


@torch.no_grad()
def convert_audio_spectogram_transformer_checkpoint(audio_spectogram_transformer_name, pytorch_dump_folder_path):
    """
    Copy/paste/tweak model's weights to our AudioSpectogramTransformer structure.
    """

    # define default AudioSpectogramTransformer configuration
    config = AudioSpectogramTransformerConfig()
    base_model = False
    # dataset (ImageNet-21k only or also fine-tuned on ImageNet 2012), patch_size and image_size
    if audio_spectogram_transformer_name[-5:] == "in21k":
        base_model = True
        config.patch_size = int(audio_spectogram_transformer_name[-12:-10])
        config.image_size = int(audio_spectogram_transformer_name[-9:-6])
    else:
        config.num_labels = 1000
        repo_id = "huggingface/label-files"
        filename = "imagenet-1k-id2label.json"
        id2label = json.load(open(hf_hub_download(repo_id, filename, repo_type="dataset"), "r"))
        id2label = {int(k): v for k, v in id2label.items()}
        config.id2label = id2label
        config.label2id = {v: k for k, v in id2label.items()}
        config.patch_size = int(audio_spectogram_transformer_name[-6:-4])
        config.image_size = int(audio_spectogram_transformer_name[-3:])
    # size of the architecture
    if "deit" in audio_spectogram_transformer_name:
        if audio_spectogram_transformer_name[9:].startswith("tiny"):
            config.hidden_size = 192
            config.intermediate_size = 768
            config.num_hidden_layers = 12
            config.num_attention_heads = 3
        elif audio_spectogram_transformer_name[9:].startswith("small"):
            config.hidden_size = 384
            config.intermediate_size = 1536
            config.num_hidden_layers = 12
            config.num_attention_heads = 6
        else:
            pass
    else:
        if audio_spectogram_transformer_name[4:].startswith("small"):
            config.hidden_size = 768
            config.intermediate_size = 2304
            config.num_hidden_layers = 8
            config.num_attention_heads = 8
        elif audio_spectogram_transformer_name[4:].startswith("base"):
            pass
        elif audio_spectogram_transformer_name[4:].startswith("large"):
            config.hidden_size = 1024
            config.intermediate_size = 4096
            config.num_hidden_layers = 24
            config.num_attention_heads = 16
        elif audio_spectogram_transformer_name[4:].startswith("huge"):
            config.hidden_size = 1280
            config.intermediate_size = 5120
            config.num_hidden_layers = 32
            config.num_attention_heads = 16

    # load original model from timm
    timm_model = timm.create_model(audio_spectogram_transformer_name, pretrained=True)
    timm_model.eval()

    # load state_dict of original model, remove and rename some keys
    state_dict = timm_model.state_dict()
    if base_model:
        remove_classification_head_(state_dict)
    rename_keys = create_rename_keys(config, base_model)
    for src, dest in rename_keys:
        rename_key(state_dict, src, dest)
    read_in_q_k_v(state_dict, config, base_model)

    # load HuggingFace model
    if audio_spectogram_transformer_name[-5:] == "in21k":
        model = AudioSpectogramTransformerModel(config).eval()
    else:
        model = AudioSpectogramTransformerForImageClassification(config).eval()
    model.load_state_dict(state_dict)

    # Check outputs on an image, prepared by AudioSpectogramTransformerFeatureExtractor/DeiTFeatureExtractor
    if "deit" in audio_spectogram_transformer_name:
        feature_extractor = DeiTFeatureExtractor(size=config.image_size)
    else:
        feature_extractor = AudioSpectogramTransformerFeatureExtractor(size=config.image_size)
    encoding = feature_extractor(images=prepare_img(), return_tensors="pt")
    pixel_values = encoding["pixel_values"]
    outputs = model(pixel_values)

    if base_model:
        timm_pooled_output = timm_model.forward_features(pixel_values)
        assert timm_pooled_output.shape == outputs.pooler_output.shape
        assert torch.allclose(timm_pooled_output, outputs.pooler_output, atol=1e-3)
    else:
        timm_logits = timm_model(pixel_values)
        assert timm_logits.shape == outputs.logits.shape
        assert torch.allclose(timm_logits, outputs.logits, atol=1e-3)

    Path(pytorch_dump_folder_path).mkdir(exist_ok=True)
    print(f"Saving model {audio_spectogram_transformer_name} to {pytorch_dump_folder_path}")
    model.save_pretrained(pytorch_dump_folder_path)
    print(f"Saving feature extractor to {pytorch_dump_folder_path}")
    feature_extractor.save_pretrained(pytorch_dump_folder_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # Required parameters
    parser.add_argument(
        "--audio_spectogram_transformer_name",
        default="audio_spectogram_transformer_base_patch16_224",
        type=str,
        help="Name of the AudioSpectogramTransformer timm model you'd like to convert.",
    )
    parser.add_argument(
        "--pytorch_dump_folder_path", default=None, type=str, help="Path to the output PyTorch model directory."
    )

    args = parser.parse_args()
    convert_audio_spectogram_transformer_checkpoint(args.audio_spectogram_transformer_name, args.pytorch_dump_folder_path)