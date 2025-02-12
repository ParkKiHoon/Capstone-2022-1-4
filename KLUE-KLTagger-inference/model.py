import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import utils
from torch.nn.parameter import Parameter
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
from transformers import AutoModel

from torch.nn import TransformerEncoder, TransformerEncoderLayer, TransformerDecoder, TransformerDecoderLayer
import math
from torch import Tensor


class AutoModelforDp(nn.Module):
    def __init__(self, config, args):
        super(AutoModelforDp, self).__init__()
        hparams = args

        self.model = AutoModel.from_config(config)

        self.hidden_size = hparams.hidden_size
        self.input_size = self.model.config.hidden_size
        self.arc_space = hparams.arc_space
        self.type_space = hparams.type_space

        self.n_pos_labels = len(utils.get_pos_labels())
        # self.n_type_labels = len(utils.get_dp_labels())
        self.n_type_labels = (
            46  # FIXME : Among all 63 types, only some of them (38) exist in klue-dp
        )
        if args.no_pos:
            self.pos_embedding = None
            self.pos_embedding2 = None
            self.pos_embedding3 = None
            # self.pos_embedding4 = None
        else:
            self.pos_embedding = nn.Embedding(self.n_pos_labels + 1, hparams.pos_dim)
            self.pos_embedding2 = nn.Embedding(self.n_pos_labels + 1, hparams.pos_dim)
            self.pos_embedding3 = nn.Embedding(self.n_pos_labels + 1, hparams.pos_dim)

        enc_dim = self.input_size * 2  # concatenate start and end subword
        if self.pos_embedding is not None:
            enc_dim += hparams.pos_dim*3


        self.Encoder_layer = TransformerEncoderLayer(d_model=enc_dim, nhead=8, dim_feedforward=self.hidden_size,
                                                     dropout=0.33)
        self.Trans_encoder = TransformerEncoder(encoder_layer=self.Encoder_layer, num_layers=hparams.encoder_layers)
        #self.pos_encoder = PositionalEncoding(d_model=2304, dropout=0.33)
        self.transformer_model = TransformerModel_Layer(d_model=2304, d_hid=768, nhead=8, nlayers=3,dropout=0.33)  # transformer layer 추가,d_model= feature개수

        self.encoder = nn.LSTM(
            enc_dim,
            self.hidden_size,
            hparams.encoder_layers,
            batch_first=True,
            dropout=0.0 if hparams.encoder_layers == 1 else 0.33,
            bidirectional=True,
        )
        self.decoder = nn.LSTM(
            self.hidden_size,
            self.hidden_size,
            hparams.decoder_layers,
            batch_first=True,
            dropout=0.0 if hparams.encoder_layers == 1 else 0.33,
        )

        self.dropout = nn.Dropout2d(p=0.33)

        self.src_dense = nn.Linear(self.hidden_size * 2, self.hidden_size)
        self.hx_dense = nn.Linear(self.hidden_size * 2, self.hidden_size)

        self.arc_c = nn.Linear(self.hidden_size * 2, self.arc_space)
        self.type_c = nn.Linear(self.hidden_size * 2, self.type_space)
        self.arc_h = nn.Linear(self.hidden_size, self.arc_space)
        self.type_h = nn.Linear(self.hidden_size, self.type_space)

        self.attention = BiAttention(self.arc_space, self.arc_space, 1)
        self.bilinear = BiLinear(self.type_space, self.type_space, self.n_type_labels)

    def forward(
        self,
        bpe_head_mask,
        bpe_tail_mask,
        pos_ids,
        pos_ids2,
        pos_ids3,
        head_ids,
        max_word_length,
        mask_e,
        mask_d,
        batch_index,
        input_ids,
        attention_mask,
    ):

        # pretrained language model
        inputs = {"input_ids": input_ids, "attention_mask": attention_mask}
        outputs = self.model(**inputs)
        outputs = outputs[0]

        # resizing outputs for top encoder-decoder layers
        outputs, sent_len = utils.resize_outputs(
            outputs, bpe_head_mask, bpe_tail_mask, max_word_length
        )

        # use pos features as inputs if available
        if self.pos_embedding is not None:
            pos_outputs = self.pos_embedding(pos_ids)  # 맨뒤
            pos_outputs2 = self.pos_embedding2(pos_ids2)  # 맨뒤 하나 앞
            pos_outputs3 = self.pos_embedding3(pos_ids3)  # 맨앞
            pos_outputs = self.dropout(pos_outputs)
            pos_outputs2 = self.dropout(pos_outputs2)
            pos_outputs3 = self.dropout(pos_outputs3)
            pos_concat = torch.cat([pos_outputs, pos_outputs2, pos_outputs3], dim=2)
            # pos_outputs_concat = self.pos_concat(pos_concat)
            # outputs = torch.cat([outputs, pos_outputs1234_2], dim=2)
            outputs = torch.cat([outputs, pos_concat], dim=2)
        """transformer layer 추가"""
        #outputs = self.transformer_model(outputs)

        # encoder
        packed_outputs = pack_padded_sequence(
            outputs, sent_len, batch_first=True, enforce_sorted=False
        )
        encoder_outputs, hn = self.encoder(packed_outputs)
        encoder_outputs, outputs_len = pad_packed_sequence(
            encoder_outputs, batch_first=True
        )
        encoder_outputs = self.dropout(encoder_outputs.transpose(1, 2)).transpose(
            1, 2
        )  # apply dropout for last layer

        hn = self._transform_decoder_init_state(hn)

        # decoder
        src_encoding = F.elu(self.src_dense(encoder_outputs[:, 1:]))
        sent_len = [i - 1 for i in sent_len]
        packed_outputs = pack_padded_sequence(
            src_encoding, sent_len, batch_first=True, enforce_sorted=False
        )
        decoder_outputs, _ = self.decoder(packed_outputs, hn)
        decoder_outputs, outputs_len = pad_packed_sequence(
            decoder_outputs, batch_first=True
        )
        decoder_outputs = self.dropout(decoder_outputs.transpose(1, 2)).transpose(
            1, 2
        )  # apply dropout for last layer

        # compute output for arc and type
        arc_c = F.elu(self.arc_c(encoder_outputs))
        type_c = F.elu(self.type_c(encoder_outputs))

        arc_h = F.elu(self.arc_h(decoder_outputs))
        type_h = F.elu(self.type_h(decoder_outputs))

        out_arc = self.attention(arc_h, arc_c, mask_d=mask_d, mask_e=mask_e).squeeze(
            dim=1
        )
        type_c = type_c[batch_index, head_ids.data.t()].transpose(0, 1).contiguous()
        out_type = self.bilinear(type_h, type_c)

        return out_arc, out_type

    def _transform_decoder_init_state(self, hn):
        hn, cn = hn
        cn = cn[-2:]  # take the last layer
        _, batch_size, hidden_size = cn.size()
        cn = cn.transpose(0, 1).contiguous()
        cn = cn.view(batch_size, 1, 2 * hidden_size).transpose(0, 1)
        cn = self.hx_dense(cn)
        if self.decoder.num_layers > 1:
            cn = torch.cat(
                [
                    cn,
                    torch.autograd.Variable(
                        cn.data.new(
                            self.decoder.num_layers - 1, batch_size, hidden_size
                        ).zero_()
                    ),
                ],
                dim=0,
            )
        hn = torch.tanh(cn)
        hn = (hn, cn)
        return hn

    @staticmethod
    def add_arguments(parser):
        parser.add_argument(
            "--max_seq_length",
            default=128,
            type=int,
            help="The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded.",
        )
        # inference-speccific args : model
        parser.add_argument(
            "--encoder_layers", default=1, type=int, help="Number of layers of encoder"
        )
        parser.add_argument(
            "--decoder_layers", default=1, type=int, help="Number of layers of decoder"
        )
        parser.add_argument(
            "--hidden_size",
            default=768,
            type=int,
            help="Number of hidden units in LSTM",
        )
        parser.add_argument(
            "--arc_space", default=512, type=int, help="Dimension of tag space"
        )
        parser.add_argument(
            "--type_space", default=256, type=int, help="Dimension of tag space"
        )
        parser.add_argument(
            "--no_pos",
            action='store_true',
            help="Use POS as input features in head layers",
        )
        parser.add_argument(
            "--pos_dim", default=256, type=int, help="Dimension of pos embedding"
        )
        args = parser.parse_args()
        if not args.no_pos and args.pos_dim <= 0:
            parser.error(
                "--pos_dim should be a positive integer when --no_pos is False."
            )
        return parser


class BiAttention(nn.Module):
    def __init__(
        self,
        input_size_encoder,
        input_size_decoder,
        num_labels,
        biaffine=True,
        **kwargs
    ):
        super(BiAttention, self).__init__()
        self.input_size_encoder = input_size_encoder
        self.input_size_decoder = input_size_decoder
        self.num_labels = num_labels
        self.biaffine = biaffine

        self.W_e = Parameter(torch.Tensor(self.num_labels, self.input_size_encoder))
        self.W_d = Parameter(torch.Tensor(self.num_labels, self.input_size_decoder))
        self.b = Parameter(torch.Tensor(self.num_labels, 1, 1))
        if self.biaffine:
            self.U = Parameter(
                torch.Tensor(
                    self.num_labels, self.input_size_decoder, self.input_size_encoder
                )
            )
        else:
            self.register_parameter("U", None)

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.xavier_uniform_(self.W_e)
        nn.init.xavier_uniform_(self.W_d)
        nn.init.constant_(self.b, 0.0)
        if self.biaffine:
            nn.init.xavier_uniform_(self.U)

    def forward(self, input_d, input_e, mask_d=None, mask_e=None):
        assert input_d.size(0) == input_e.size(0)
        batch, length_decoder, _ = input_d.size()
        _, length_encoder, _ = input_e.size()

        out_d = torch.matmul(self.W_d, input_d.transpose(1, 2)).unsqueeze(3)
        out_e = torch.matmul(self.W_e, input_e.transpose(1, 2)).unsqueeze(2)

        if self.biaffine:
            output = torch.matmul(input_d.unsqueeze(1), self.U)
            output = torch.matmul(output, input_e.unsqueeze(1).transpose(2, 3))
            output = output + out_d + out_e + self.b
        else:
            output = out_d + out_d + self.b

        if mask_d is not None:
            output = (
                output
                * mask_d.unsqueeze(1).unsqueeze(3)
                * mask_e.unsqueeze(1).unsqueeze(2)
            )

        return output


class BiLinear(nn.Module):
    def __init__(self, left_features, right_features, out_features):
        super(BiLinear, self).__init__()
        self.left_features = left_features
        self.right_features = right_features
        self.out_features = out_features

        self.U = Parameter(
            torch.Tensor(self.out_features, self.left_features, self.right_features)
        )
        self.W_l = Parameter(torch.Tensor(self.out_features, self.left_features))
        self.W_r = Parameter(torch.Tensor(self.out_features, self.left_features))
        self.bias = Parameter(torch.Tensor(out_features))

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.xavier_uniform_(self.W_l)
        nn.init.xavier_uniform_(self.W_r)
        nn.init.constant_(self.bias, 0.0)
        nn.init.xavier_uniform_(self.U)

    def forward(self, input_left, input_right):
        left_size = input_left.size()
        right_size = input_right.size()
        assert (
            left_size[:-1] == right_size[:-1]
        ), "batch size of left and right inputs mis-match: (%s, %s)" % (
            left_size[:-1],
            right_size[:-1],
        )
        batch = int(np.prod(left_size[:-1]))

        input_left = input_left.reshape(batch, self.left_features)
        input_right = input_right.reshape(batch, self.right_features)

        output = F.bilinear(input_left, input_right, self.U, self.bias)
        output = (
            output
            + F.linear(input_left, self.W_l, None)
            + F.linear(input_right, self.W_r, None)
        )
        return output.view(left_size[:-1] + (self.out_features,))


class TransformerModel_Layer(nn.Module):

    def __init__(self, d_model: int, nhead: int, d_hid: int,
                 nlayers: int, dropout: float = 0.33):
        super().__init__()
        self.model_type = 'Transformer'
        #self.pos_encoder = PositionalEncoding(d_model, dropout)
        encoder_layers = TransformerEncoderLayer(d_model, nhead, d_hid, dropout)
        self.transformer_encoder = TransformerEncoder(encoder_layers, nlayers)
        #self.encoder = nn.Embedding(ntoken, d_model)
        #self.d_model = d_model
        #self.decoder = nn.Linear(d_model, ntoken)

        #self.init_weights()

    def init_weights(self) -> None:
        initrange = 0.1
        self.encoder.weight.data.uniform_(-initrange, initrange)
        #self.decoder.bias.data.zero_()
        #self.decoder.weight.data.uniform_(-initrange, initrange)

    def forward(self, src: Tensor) -> Tensor:
        """
        Args:
            src: Tensor, shape [seq_len, batch_size]
            src_mask: Tensor, shape [seq_len, seq_len]

        Returns:
            output Tensor of shape [seq_len, batch_size, ntoken]
        """
        #src = self.encoder(src) * math.sqrt(self.d_model)
        #src = self.pos_encoder(src)
        output = self.transformer_encoder(src)
        #output = self.decoder(output)
        return output

class PositionalEncoding(nn.Module):

    def __init__(self, d_model: int, dropout: float = 0.1, max_len: int = 5000):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        position = torch.arange(max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2) * (-math.log(10000.0) / d_model))
        pe = torch.zeros(max_len, 1, d_model)
        pe[:, 0, 0::2] = torch.sin(position * div_term)
        pe[:, 0, 1::2] = torch.cos(position * div_term)
        self.register_buffer('pe', pe)

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: Tensor, shape [seq_len, batch_size, embedding_dim]
        """
        x = x + self.pe[:x.size(0)]
        return self.dropout(x)