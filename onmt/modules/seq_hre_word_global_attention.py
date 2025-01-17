"""Global attention modules (Luong / Bahdanau)"""
import torch
import torch.nn as nn
import torch.nn.functional as F

from onmt.modules.sparse_activations import sparsemax
from onmt.utils.misc import aeq, sequence_mask

# This class is mainly used by decoder.py for RNNs but also
# by the CNN / transformer decoder when copy attention is used
# CNN has its own attention mechanism ConvMultiStepAttention
# Transformer has its own MultiHeadedAttention


class SeqHREWordGlobalAttention(nn.Module):
    r"""
    Global attention takes a matrix and a query vector. It
    then computes a parameterized convex combination of the matrix
    based on the input query.

    Constructs a unit mapping a query `q` of size `dim`
    and a source matrix `H` of size `n x dim`, to an output
    of size `dim`.


    .. mermaid::

       graph BT
          A[Query]
          subgraph RNN
            C[H 1]
            D[H 2]
            E[H N]
          end
          F[Attn]
          G[Output]
          A --> F
          C --> F
          D --> F
          E --> F
          C -.-> G
          D -.-> G
          E -.-> G
          F --> G

    All models compute the output as
    :math:`c = \sum_{j=1}^{\text{SeqLength}} a_j H_j` where
    :math:`a_j` is the softmax of a score function.
    Then then apply a projection layer to [q, c].

    However they
    differ on how they compute the attention score.

    * Luong Attention (dot, general):
       * dot: :math:`\text{score}(H_j,q) = H_j^T q`
       * general: :math:`\text{score}(H_j, q) = H_j^T W_a q`


    * Bahdanau Attention (mlp):
       * :math:`\text{score}(H_j, q) = v_a^T \text{tanh}(W_a q + U_a h_j)`


    Args:
       dim (int): dimensionality of query and key
       coverage (bool): use coverage term
       attn_type (str): type of attention to use, options [dot,general,mlp]
       attn_func (str): attention function to use, options [softmax,sparsemax]

    """

    def __init__(self, dim, coverage=False, attn_type="dot",
                 attn_func="softmax", output_attn_h=False, seqHRE_attn_rescale=False):
        super(SeqHREWordGlobalAttention, self).__init__()

        self.dim = dim
        self.seqHRE_attn_rescale = seqHRE_attn_rescale
        assert attn_type in ["dot", "general", "mlp"], (
            "Please select a valid attention type (got {:s}).".format(
                attn_type))
        self.attn_type = attn_type
        assert attn_func in ["softmax", "sparsemax"], (
            "Please select a valid attention function.")
        self.attn_func = attn_func

        if self.attn_type == "general":
            self.linear_in = nn.Linear(dim, dim, bias=False)
        elif self.attn_type == "mlp":
            self.linear_context = nn.Linear(dim, dim, bias=False)
            self.linear_query = nn.Linear(dim, dim, bias=True)
            self.v = nn.Linear(dim, 1, bias=False)

        self.output_attn_h = output_attn_h
        if output_attn_h:
            # mlp wants it with bias
            out_bias = self.attn_type == "mlp"
            self.linear_out = nn.Linear(dim * 2, dim, bias=out_bias)

        if coverage:
            self.linear_cover = nn.Linear(1, dim, bias=False)

    def score(self, h_t, h_s):
        """
        Args:
          h_t (FloatTensor): sequence of queries ``(batch, tgt_len, dim)``
          h_s (FloatTensor): sequence of sources ``(batch, src_len, dim``

        Returns:
          FloatTensor: raw attention scores (unnormalized) for each src index
            ``(batch, tgt_len, src_len)``
        """

        # Check input sizes
        src_batch, src_len, src_dim = h_s.size()
        tgt_batch, tgt_len, tgt_dim = h_t.size()
        aeq(src_batch, tgt_batch)
        aeq(src_dim, tgt_dim)
        aeq(self.dim, src_dim)

        if self.attn_type in ["general", "dot"]:
            if self.attn_type == "general":
                h_t_ = h_t.view(tgt_batch * tgt_len, tgt_dim)
                h_t_ = self.linear_in(h_t_)
                h_t = h_t_.view(tgt_batch, tgt_len, tgt_dim)
            h_s_ = h_s.transpose(1, 2)
            # (batch, t_len, d) x (batch, d, s_len) --> (batch, t_len, s_len)
            return torch.bmm(h_t, h_s_)
        else:
            dim = self.dim
            wq = self.linear_query(h_t.view(-1, dim))
            wq = wq.view(tgt_batch, tgt_len, 1, dim)
            wq = wq.expand(tgt_batch, tgt_len, src_len, dim)

            uh = self.linear_context(h_s.contiguous().view(-1, dim))
            uh = uh.view(src_batch, 1, src_len, dim)
            uh = uh.expand(src_batch, tgt_len, src_len, dim)

            # (batch, t_len, s_len, d)
            wquh = torch.tanh(wq + uh)

            return self.v(wquh.view(-1, dim)).view(tgt_batch, tgt_len, src_len)

    def forward(self, source, memory_bank, memory_lengths=None, coverage=None,
                utr_align_vectors=None, utr_position_tuple=None, src_word_utr_ids=None):
        """

        Args:
          source (FloatTensor): query vectors ``(batch, tgt_len, dim)``
          memory_bank (FloatTensor): source vectors ``(batch, src_len, dim)``
          memory_lengths (LongTensor): the source context lengths ``(batch,)``
          coverage (FloatTensor): None (not supported yet)
          utr_align_vectors (`FloatTensor`): the computed sentence align distribution, `[batch x tgt_len x s_num]`
                or `[batch x s_num]` for one step
          utr_position_tuple (:obj: `tuple`): Only used for seqhr_enc (utr_p, utr_nums) with size
                `([batch_size, s_num, 2], [batch])`.
          src_word_utr_ids (:obj: `tuple'): (word_utr_ids, src_lengths) with size `([batch, src_len], [batch])'

        Returns:
          (FloatTensor, FloatTensor):

          * Computed vector ``(tgt_len, batch, dim)``
          * Attention distribtutions for each query
            ``(tgt_len, batch, src_len)``
        """

        # one step input
        if source.dim() == 2:
            one_step = True
            source = source.unsqueeze(1)
        else:
            one_step = False

        if utr_align_vectors.dim() == 2:
            utr_align_vectors = utr_align_vectors.unsqueeze(1)

        batch, source_l, dim = memory_bank.size()
        batch_, target_l, dim_ = source.size()
        batch__, target_l_, s_sum = utr_align_vectors.size()
        aeq(batch, batch_, batch__)
        aeq(dim, dim_)
        aeq(self.dim, dim)
        aeq(target_l, target_l_)

        # check the specification for word level attention
        assert utr_align_vectors is not None, "For word level attention, the 'sent_align' must be specified."
        assert memory_lengths is not None, "The lengths for the word memory bank are required."

        if coverage is not None:
            batch_, source_l_ = coverage.size()
            aeq(batch, batch_)
            aeq(source_l, source_l_)

        if coverage is not None:
            cover = coverage.view(-1).unsqueeze(1)
            memory_bank += self.linear_cover(cover).view_as(memory_bank)
            memory_bank = torch.tanh(memory_bank)

        # compute attention scores, as in Luong et al.
        # [batch, tgt_len, src_len]
        word_align = self.score(source, memory_bank)

        if memory_lengths is not None:
            mask = sequence_mask(memory_lengths, max_len=word_align.size(-1))
            mask = mask.unsqueeze(1)  # Make it broadcastable.
            word_align.masked_fill_(~mask, -float('inf'))

        # Softmax or sparsemax to normalize attention weights
        if self.attn_func == "softmax":
            align_vectors = F.softmax(word_align.view(batch*target_l, source_l), -1)
        else:
            align_vectors = sparsemax(word_align.view(batch*target_l, source_l), -1)
        align_vectors = align_vectors.view(batch, target_l, source_l)

        if self.seqHRE_attn_rescale:
            word_utr_ids, memory_lengths_ = src_word_utr_ids
            assert memory_lengths.eq(memory_lengths_).all(), \
                "The src lengths in src_word_utr_ids should be the same as the memory_lengths"
            # attention score reweighting method 2
            # word_utr_ids: [batch, src_len]->[batch, tgt_len, src_len]
            # utr_align_vectors: [batch, tgt_len, utr_num]
            # expand_utr_align_vectors: [batch, tgt_len, src_len]
            # TODO: check the correctness
            word_utr_ids = word_utr_ids.unsqueeze(1).expand(-1, target_l, -1)
            expand_utr_align_vectors = utr_align_vectors.gather(dim=-1, index=word_utr_ids)
            # # reweight and renormalize the word align_vectors
            # Although word_utr_ids are padded with 0s which will gather the attention score of the sentence 0
            # align_vectors are 0.0000 on these padded places.
            align_vectors = align_vectors * expand_utr_align_vectors
            norm_term = align_vectors.sum(dim=-1, keepdim=True)
            align_vectors = align_vectors / norm_term

        # each context vector c_t is the weighted average
        # over all the source hidden states
        # [batch_size, tgt_len, dim]
        c = torch.bmm(align_vectors, memory_bank)

        attn_h = None
        # If output_attn_h, we put linear out layer on decoder part
        if self.output_attn_h:
            # concatenate
            concat_c = torch.cat([c, source], 2).view(batch*target_l, dim*2)
            attn_h = self.linear_out(concat_c).view(batch, target_l, dim)
            if self.attn_type in ["general", "dot"]:
                attn_h = torch.tanh(attn_h)

        if one_step:
            c = c.squeeze(1)
            batch_, dim_ = c.size()
            if self.output_attn_h:
                attn_h = attn_h.squeeze(1)
                batch_, dim_ = attn_h.size()
            align_vectors = align_vectors.squeeze(1)

            # Check output sizes
            aeq(batch, batch_)
            aeq(dim, dim_)
            batch_, source_l_ = align_vectors.size()
            aeq(batch, batch_)
            aeq(source_l, source_l_)

        else:
            c = c.transpose(0, 1).contiguous()
            target_l_, batch_, dim_ = c.size()
            if self.output_attn_h:
                attn_h = attn_h.transpose(0, 1).contiguous()
                target_l_, batch_, dim_ = attn_h.size()
            align_vectors = align_vectors.transpose(0, 1).contiguous()
            # Check output sizes
            aeq(target_l, target_l_)
            aeq(batch, batch_)
            aeq(dim, dim_)
            target_l_, batch_, source_l_ = align_vectors.size()
            aeq(target_l, target_l_)
            aeq(batch, batch_)
            aeq(source_l, source_l_)

        return c, attn_h, align_vectors
