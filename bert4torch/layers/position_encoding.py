from torch import nn
import torch
import math
import torch.nn.functional as F
from bert4torch.snippets import get_sinusoid_encoding_table


class RelativePositionsEncodingDebertaV2(nn.Module):
    """deberta用的相对位置编码
    来自论文：https://arxiv.org/abs/2006.03654
    """
    def __init__(self, qlen, klen, position_buckets, max_position):
        super(RelativePositionsEncodingDebertaV2, self).__init__()
        q_ids = torch.arange(0, qlen)
        k_ids = torch.arange(0, klen)
        rel_pos_ids = q_ids[:, None] - k_ids[None, :]
        if position_buckets > 0 and max_position > 0:
            rel_pos_ids = self.make_log_bucket_position(rel_pos_ids, position_buckets, max_position)
        rel_pos_ids = rel_pos_ids.to(torch.long)
        rel_pos_ids = rel_pos_ids[:qlen, :]
        rel_pos_ids = rel_pos_ids.unsqueeze(0)
        self.register_buffer('relative_position', rel_pos_ids)

    @staticmethod
    def make_log_bucket_position(relative_pos, bucket_size, max_position):
        sign = torch.sign(relative_pos)
        mid = bucket_size // 2
        abs_pos = torch.where((relative_pos < mid) & (relative_pos > -mid),
            torch.tensor(mid - 1).type_as(relative_pos),
            torch.abs(relative_pos),
        )
        log_pos = (
            torch.ceil(torch.log(abs_pos / mid) / torch.log(torch.tensor((max_position - 1) / mid)) * (mid - 1)) + mid
        )
        bucket_pos = torch.where(abs_pos <= mid, relative_pos.type_as(log_pos), log_pos * sign)
        return bucket_pos

    def forward(self, qlen, klen):
        return self.relative_position[:, :qlen, :klen]


class RelativePositionsEncoding(nn.Module):
    """nezha用的google相对位置编码
    来自论文：https://arxiv.org/abs/1803.02155
    """
    def __init__(self, qlen, klen, embedding_size, max_relative_position=127):
        super(RelativePositionsEncoding, self).__init__()
        # 生成相对位置矩阵
        vocab_size = max_relative_position * 2 + 1
        distance_mat = torch.arange(klen)[None, :] - torch.arange(qlen)[:, None]  # 列数-行数, [query_len, key_len]
        distance_mat_clipped = torch.clamp(distance_mat, -max_relative_position, max_relative_position)
        final_mat = distance_mat_clipped + max_relative_position

        # sinusoid_encoding编码的位置矩阵
        embeddings_table = get_sinusoid_encoding_table(vocab_size, embedding_size)

        # 实现方式1
        # flat_relative_positions_matrix = final_mat.view(-1)
        # one_hot_relative_positions_matrix = torch.nn.functional.one_hot(flat_relative_positions_matrix, num_classes=vocab_size).float()
        # position_embeddings = torch.matmul(one_hot_relative_positions_matrix, embeddings_table)
        # my_shape = list(final_mat.size())
        # my_shape.append(embedding_size)
        # position_embeddings = position_embeddings.view(my_shape)

        # 实现方式2
        # position_embeddings = take_along_dim(embeddings_table, final_mat.flatten().unsqueeze(1), dim=0)
        # position_embeddings = position_embeddings.reshape(*final_mat.shape, embeddings_table.shape[-1])  # [seq_len, seq_len, hdsz]
        # self.register_buffer('position_embeddings', position_embeddings)
        
        # 实现方式3
        position_embeddings = nn.Embedding.from_pretrained(embeddings_table, freeze=True)(final_mat)
        self.register_buffer('position_embeddings', position_embeddings)

    def forward(self, qlen, klen):
        return self.position_embeddings[:qlen, :klen, :]


class RelativePositionsEncodingT5(nn.Module):
    """Google T5的相对位置编码
    来自论文：https://arxiv.org/abs/1910.10683
    """
    def __init__(self, qlen, klen, relative_attention_num_buckets, is_decoder=False):
        super(RelativePositionsEncodingT5, self).__init__()
        # 生成相对位置矩阵
        context_position = torch.arange(qlen, dtype=torch.long)[:, None]
        memory_position = torch.arange(klen, dtype=torch.long)[None, :]
        relative_position = memory_position - context_position  # shape (qlen, klen)
        relative_position = self._relative_position_bucket(
            relative_position,  # shape (qlen, klen)
            bidirectional=not is_decoder,
            num_buckets=relative_attention_num_buckets,
        )
        self.register_buffer('relative_position', relative_position)

    def forward(self, qlen, klen):
        return self.relative_position[:qlen, :klen]

    @staticmethod
    def _relative_position_bucket(relative_position, bidirectional=True, num_buckets=32, max_distance=128):
        '''直接来源于transformer
        '''
        ret = 0
        n = -relative_position
        if bidirectional:
            num_buckets //= 2
            ret += (n < 0).to(torch.long) * num_buckets  # mtf.to_int32(mtf.less(n, 0)) * num_buckets
            n = torch.abs(n)
        else:
            n = torch.max(n, torch.zeros_like(n))
        # now n is in the range [0, inf)

        # half of the buckets are for exact increments in positions
        max_exact = num_buckets // 2
        is_small = n < max_exact

        # The other half of the buckets are for logarithmically bigger bins in positions up to max_distance
        val_if_large = max_exact + (
            torch.log(n.float() / max_exact) / math.log(max_distance / max_exact) * (num_buckets - max_exact)
        ).to(torch.long)
        val_if_large = torch.min(val_if_large, torch.full_like(val_if_large, num_buckets - 1))

        ret += torch.where(is_small, n, val_if_large)
        return ret


class SinusoidalPositionEncoding(nn.Module):
    """定义Sin-Cos位置Embedding
    """
    def __init__(self, max_position, embedding_size):
        super(SinusoidalPositionEncoding, self).__init__()
        self.position_embeddings = nn.Embedding.from_pretrained(get_sinusoid_encoding_table(max_position, embedding_size), freeze=True) 
    def forward(self, position_ids):
        return self.position_embeddings(position_ids)


class RoPEPositionEncoding(nn.Module):
    """旋转式位置编码: https://kexue.fm/archives/8265
    """
    def __init__(self, embedding_size, rope_rank='adjacent', **kwargs):
        super(RoPEPositionEncoding, self).__init__()
        self.max_seq_len_cache = -1
        self.embedding_size = embedding_size
        # 支持两种方式，一种是奇偶相邻排列，一种是上下排列, 目前只在chatglm中看到updown排列
        assert rope_rank in {'adjacent', 'updown'}, "rank kwarg only support 'adjacent' and 'updown' "
        self.rope_rank = rope_rank
    
    def initialize(self, max_position):
        position_embeddings = get_sinusoid_encoding_table(max_position, self.embedding_size)  # [seq_len, hdsz]
        if self.rope_rank == 'adjacent':
            cos_position = position_embeddings[:, 1::2].repeat_interleave(2, dim=-1)  # [seq_len, hdsz]
            sin_position = position_embeddings[:, ::2].repeat_interleave(2, dim=-1)  # [seq_len, hdsz]
        elif self.rope_rank == 'updown':  # 目前仅chatglm使用
            cos_position = position_embeddings[:, 1::2].repeat(1,2)  # [seq_len, hdsz]
            sin_position = position_embeddings[:, ::2].repeat(1,2)  # [seq_len, hdsz]
        else:
            raise ValueError('Args `rope_rank` only support `adjacent` and `updown` mode')
        return cos_position, sin_position
    
    def forward(self, qw, position_ids=None, seq_dim=-2):
        # MultiHeadAttentionLayer中qw是[btz, n_heads, seq_len, head_size]
        # GlobalPointer中*转置*后qw是[btz, n_heads, seq_len, head_size]
        # EfficientGlobalPointer中qw是[btz, seq_len, head_size]
        if self.rope_rank == 'adjacent':
            qw2 = torch.stack([-qw[..., 1::2], qw[..., ::2]], dim=-1).reshape_as(qw)
        elif self.rope_rank == 'updown':  # 目前仅chatglm使用
            qw2 = torch.cat([-qw[..., qw.shape[-1]//2:], qw[..., :qw.shape[-1]//2]], dim=-1)  # cat和stack+reshape是结果不同的
        
        # 超过缓存长度
        seq_len = position_ids.max() + 1 if position_ids is not None else qw.shape[seq_dim]
        if seq_len > self.max_seq_len_cache:
            cos_position, sin_position = self.initialize(seq_len)
            self.cos_position, self.sin_position = cos_position.type_as(qw).to(qw.device), sin_position.type_as(qw).to(qw.device)
            self.max_seq_len_cache = seq_len

        # 传入position_ids来获取cos和sin, 主要是在use_states时候能直接取到对应位置的编码
        if position_ids is not None:
            # position_ids: [btz, seq_len]
            cos = F.embedding(position_ids, self.cos_position)  # [btz, seq_len, hdsz]
            sin = F.embedding(position_ids, self.sin_position)
        else:
            cos = self.cos_position[:seq_len]  # [seq_len, hdsz]
            sin = self.sin_position[:seq_len]

        if cos.dim() < qw.dim():
            cos = cos.unsqueeze(seq_dim-1)
            sin = sin.unsqueeze(seq_dim-1)
        return qw * cos + qw2 * sin


class XlnetPositionsEncoding(nn.Module):
    '''Xlnet, transformer_xl使用的相对位置编码
       和SinusoidalPositionEncoding区别是一个是间隔排列, 一个是前后排列
    '''
    def __init__(self, embedding_size):
        super().__init__()
        self.demb = embedding_size
        inv_freq = 1 / (10000 ** (torch.arange(0.0, embedding_size, 2.0) / embedding_size))
        self.register_buffer("inv_freq", inv_freq)

    def forward(self, pos_seq):
        sinusoid_inp = torch.ger(pos_seq, self.inv_freq)
        pos_emb = torch.cat([sinusoid_inp.sin(), sinusoid_inp.cos()], dim=-1)
        return pos_emb