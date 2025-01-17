# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math
from typing import Any, Dict, List, Optional, Tuple

import torch
import torch.nn as nn
from fairseq import utils
from fairseq.distributed import fsdp_wrap
from fairseq.models.transformer import (
    Embedding,
    TransformerDecoder,
    TransformerEncoder,
    TransformerModel,
)
from fairseq.models import (
    FairseqEncoder,
    FairseqEncoderDecoderModel,
    FairseqIncrementalDecoder,
    register_model,
    register_model_architecture,
)
from fairseq.modules import (
    AdaptiveSoftmax,
    BaseLayer,
    FairseqDropout,
    LayerDropModuleList,
    LayerNorm,
    PositionalEmbedding,
    SinusoidalPositionalEmbedding,
    TransformerDecoderLayer,
    TransformerEncoderLayer,
    TransformerAdaptiveMultiscaleEncoderLayer,
)
from fairseq.modules.checkpoint_activations import checkpoint_wrapper
from fairseq.modules.quant_noise import quant_noise as apply_quant_noise_
from torch import Tensor
import torch.nn.functional as F
from fairseq.data.encoders.gpt2_bpe import GPT2BPE
import numpy as np
import re
import pdb
import random

DEFAULT_MAX_SOURCE_POSITIONS = 4096
DEFAULT_MAX_TARGET_POSITIONS = 4096


DEFAULT_MIN_PARAMS_TO_WRAP = int(1e8)

# This model is not mentioned in our paper, but we tried this because it is a mixture of MSC and Ada-MSHA.
# Basically, we add the adaptive selection function to MSC.
# A major difference between this and Ada-MSHA is Ada-MSHA conduct contextualization on attention heads, 
# but this model counduct contextualization on hidden state
@register_model("adaptive_multiscale_transformer")
class MultiscaleHeadTransformerModel(TransformerModel):
    @staticmethod
    def add_args(parser):
        parser.add_argument(
            '--conv-kernels',
            type=str,
            default="0 1 3 5 7",
            help="kernel size of cnns, should be space separated"
        )
        parser.add_argument(
            "--apply-to-decoder",
            default=False,
            action="store_true",
            help="default setting is only on encoder, because the problems on decoder are not fixed yet"
        )
        parser.add_argument(
            "--left-pad-encoder",
            default=False,
            action="store_true",
            help="default setting for encoder is padding on both side, set this to activate left pad"
        )
        parser.add_argument(
            "--ms-layers",
            type=int,
            default=-1,
            help="the number of multi-scale layers. the rest layers are normal ones"
        )
        parser.add_argument(
            "--print-expert-weight",
            default=False,
            action="store_true",
            help="print the selected numbers of each experts"
        )
        parser.add_argument(
            "--token-level-adaptive",
            default=False,
            action="store_true",
            help="token level adaptive"
        )
        parser.add_argument(
            "--langid-expert",
            default=False,
            action="store_true",
            help="use lid to assist expert choice"
        )
        TransformerModel.add_args(parser)

    @classmethod
    def build_encoder(cls, args, src_dict, embed_tokens):
        return TransformerMultiscaleHeadEncoder(args, src_dict, embed_tokens)

class TransformerMultiscaleHeadEncoder(TransformerEncoder):
    def __init__(self, args, dictionary, embed_tokens):
        self.args = args
        super().__init__(args, dictionary, embed_tokens)

        self.langid_expert = getattr(args, "langid_expert", False)
        self.register_buffer("version", torch.Tensor([3]))

        self.dropout_module = FairseqDropout(
            args.dropout, module_name=self.__class__.__name__
        )
        self.encoder_layerdrop = args.encoder_layerdrop

        embed_dim = embed_tokens.embedding_dim
        self.embed_dim = embed_dim
        self.padding_idx = embed_tokens.padding_idx
        self.max_source_positions = args.max_source_positions

        self.embed_tokens = embed_tokens

        self.embed_scale = 1.0 if args.no_scale_embedding else math.sqrt(embed_dim)

        self.embed_positions = (
            PositionalEmbedding(
                args.max_source_positions,
                embed_dim,
                self.padding_idx,
                learned=args.encoder_learned_pos,
            )
            if not args.no_token_positional_embeddings
            else None
        )

        if getattr(args, "layernorm_embedding", False):
            self.layernorm_embedding = LayerNorm(embed_dim)
        else:
            self.layernorm_embedding = None

        if not args.adaptive_input and args.quant_noise_pq > 0:
            self.quant_noise = apply_quant_noise_(
                nn.Linear(embed_dim, embed_dim, bias=False),
                args.quant_noise_pq,
                args.quant_noise_pq_block_size,
            )
        else:
            self.quant_noise = None

        if self.encoder_layerdrop > 0.0:
            self.layers = LayerDropModuleList(p=self.encoder_layerdrop)
        else:
            self.layers = nn.ModuleList([])
        ms_layers = getattr(args, "ms_layers", -1)
        if ms_layers==-1:
            ms_layers=args.encoder_layers
        assert 0<=ms_layers<=args.encoder_layers
        self.layers.extend(
            [self.build_encoder_layer(args) for i in range(ms_layers)]
        )
        self.layers.extend(
            [self.build_encoder_layer(args, normal_layer=True) for i in range(args.encoder_layers-ms_layers)]
        )
        self.num_layers = len(self.layers)

        if args.encoder_normalize_before:
            self.layer_norm = LayerNorm(embed_dim)
        else:
            self.layer_norm = None

    def build_encoder_layer(self, args, normal_layer=False):
        if normal_layer==True:
            layer = TransformerEncoderLayer(args)
        else:
            layer = TransformerAdaptiveMultiscaleEncoderLayer(args)
        checkpoint = getattr(args, "checkpoint_activations", False)
        if checkpoint:
            offload_to_cpu = getattr(args, "offload_activations", False)
            layer = checkpoint_wrapper(layer, offload_to_cpu=offload_to_cpu)
        # if we are checkpointing, enforce that FSDP always wraps the
        # checkpointed layer, regardless of layer size
        min_params_to_wrap = (
            getattr(args, "min_params_to_wrap", DEFAULT_MIN_PARAMS_TO_WRAP)
            if not checkpoint else 0
        )
        layer = fsdp_wrap(layer, min_num_params=min_params_to_wrap)
        return layer

    def forward_scriptable(
        self,
        src_tokens,
        src_lengths: Optional[torch.Tensor] = None,
        return_all_hiddens: bool = False,
        token_embeddings: Optional[torch.Tensor] = None,
    ):
        """
        Args:
            src_tokens (LongTensor): tokens in the source language of shape
                `(batch, src_len)`
            src_lengths (torch.LongTensor): lengths of each source sentence of
                shape `(batch)`
            return_all_hiddens (bool, optional): also return all of the
                intermediate hidden states (default: False).
            token_embeddings (torch.Tensor, optional): precomputed embeddings
                default `None` will recompute embeddings

        Returns:
            dict:
                - **encoder_out** (Tensor): the last encoder layer's output of
                  shape `(src_len, batch, embed_dim)`
                - **encoder_padding_mask** (ByteTensor): the positions of
                  padding elements of shape `(batch, src_len)`
                - **encoder_embedding** (Tensor): the (scaled) embedding lookup
                  of shape `(batch, src_len, embed_dim)`
                - **encoder_states** (List[Tensor]): all intermediate
                  hidden states of shape `(src_len, batch, embed_dim)`.
                  Only populated if *return_all_hiddens* is True.
        """
        if self.langid_expert:
            # locate the lang id. In fairseq, this should be the first token with token_id > 3
            unprint_sign_mask = src_tokens.gt(3)
            src_tokens_masked = src_tokens.masked_fill(~unprint_sign_mask, -1)
            langid_indices = torch.argmax(src_tokens_masked, dim=1)
            langids = src_tokens[torch.arange(src_tokens.shape[0]), langid_indices]
            # langid_embeddings.shape = bsz * dim
            langid_embeddings = self.embed_tokens(langids)
        else:
            langid_embeddings = None
        # compute padding mask
        encoder_padding_mask = src_tokens.eq(self.padding_idx)
        has_pads = (src_tokens.device.type == "xla" or encoder_padding_mask.any())

        x, encoder_embedding = self.forward_embedding(src_tokens, token_embeddings)

        # account for padding while computing the representation
        if has_pads:
            x = x * (1 - encoder_padding_mask.unsqueeze(-1).type_as(x))

        # B x T x C -> T x B x C
        x = x.transpose(0, 1)

        encoder_states = []

        if return_all_hiddens:
            encoder_states.append(x)

        # encoder layers
        for layer in self.layers:
            if isinstance(layer, TransformerAdaptiveMultiscaleEncoderLayer):
                lr = layer(
                    x, encoder_padding_mask=encoder_padding_mask if has_pads else None, langid_embeddings=langid_embeddings
                )
            else:
                lr = layer(
                    x, encoder_padding_mask=encoder_padding_mask if has_pads else None
                )
            if isinstance(lr, tuple) and len(lr) == 2:
                x, fc_result = lr
            else:
                x = lr
                fc_result = None
                
            if return_all_hiddens:
                assert encoder_states is not None
                encoder_states.append(x)

        if self.layer_norm is not None:
            x = self.layer_norm(x)

        # The Pytorch Mobile lite interpreter does not supports returning NamedTuple in
        # `forward` so we use a dictionary instead.
        # TorchScript does not support mixed values so the values are all lists.
        # The empty list is equivalent to None.
        return {
            "encoder_out": [x],  # T x B x C
            "encoder_padding_mask": [encoder_padding_mask],  # B x T
            "encoder_embedding": [encoder_embedding],  # B x T x C
            "encoder_states": encoder_states,  # List[T x B x C]
            "src_tokens": [],
            "src_lengths": [],
        }

@register_model_architecture("adaptive_multiscale_transformer", "adaptive_multiscale_transformer")
def base_architecture(args):
    args.encoder_embed_path = getattr(args, "encoder_embed_path", None)
    args.encoder_embed_dim = getattr(args, "encoder_embed_dim", 512)
    args.encoder_ffn_embed_dim = getattr(args, "encoder_ffn_embed_dim", 2048)
    args.encoder_layers = getattr(args, "encoder_layers", 6)
    args.encoder_attention_heads = getattr(args, "encoder_attention_heads", 8)
    args.encoder_normalize_before = getattr(args, "encoder_normalize_before", False)
    args.encoder_learned_pos = getattr(args, "encoder_learned_pos", False)
    args.decoder_embed_path = getattr(args, "decoder_embed_path", None)
    args.decoder_embed_dim = getattr(args, "decoder_embed_dim", args.encoder_embed_dim)
    args.decoder_ffn_embed_dim = getattr(
        args, "decoder_ffn_embed_dim", args.encoder_ffn_embed_dim
    )
    args.decoder_layers = getattr(args, "decoder_layers", 6)
    args.decoder_attention_heads = getattr(args, "decoder_attention_heads", 8)
    args.decoder_normalize_before = getattr(args, "decoder_normalize_before", False)
    args.decoder_learned_pos = getattr(args, "decoder_learned_pos", False)
    args.attention_dropout = getattr(args, "attention_dropout", 0.0)
    args.activation_dropout = getattr(args, "activation_dropout", 0.0)
    args.activation_fn = getattr(args, "activation_fn", "relu")
    args.dropout = getattr(args, "dropout", 0.1)
    args.adaptive_softmax_cutoff = getattr(args, "adaptive_softmax_cutoff", None)
    args.adaptive_softmax_dropout = getattr(args, "adaptive_softmax_dropout", 0)
    args.share_decoder_input_output_embed = getattr(
        args, "share_decoder_input_output_embed", False
    )
    args.share_all_embeddings = getattr(args, "share_all_embeddings", False)
    args.no_token_positional_embeddings = getattr(
        args, "no_token_positional_embeddings", False
    )
    args.adaptive_input = getattr(args, "adaptive_input", False)
    args.no_cross_attention = getattr(args, "no_cross_attention", False)
    args.cross_self_attention = getattr(args, "cross_self_attention", False)

    args.decoder_output_dim = getattr(
        args, "decoder_output_dim", args.decoder_embed_dim
    )
    args.decoder_input_dim = getattr(args, "decoder_input_dim", args.decoder_embed_dim)

    args.no_scale_embedding = getattr(args, "no_scale_embedding", False)
    args.layernorm_embedding = getattr(args, "layernorm_embedding", False)
    args.tie_adaptive_weights = getattr(args, "tie_adaptive_weights", False)
    args.checkpoint_activations = getattr(args, "checkpoint_activations", False)
    args.offload_activations = getattr(args, "offload_activations", False)
    if args.offload_activations:
        args.checkpoint_activations = True
    args.encoder_layers_to_keep = getattr(args, "encoder_layers_to_keep", None)
    args.decoder_layers_to_keep = getattr(args, "decoder_layers_to_keep", None)
    args.encoder_layerdrop = getattr(args, "encoder_layerdrop", 0)
    args.decoder_layerdrop = getattr(args, "decoder_layerdrop", 0)
    args.quant_noise_pq = getattr(args, "quant_noise_pq", 0)
    args.quant_noise_pq_block_size = getattr(args, "quant_noise_pq_block_size", 8)
    args.quant_noise_scalar = getattr(args, "quant_noise_scalar", 0)