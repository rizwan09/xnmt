import math
import numbers

import numpy as np
import dynet as dy

from xnmt import logger
from xnmt import batchers, expression_seqs, events, param_collections, param_initializers
from xnmt.persistence import serializable_init, Serializable, Ref, bare

class AttenderState(object):
  pass

class Attender(object):
  """
  A template class for functions implementing attention.
  """

  def init_sent(self, sent: expression_seqs.ExpressionSequence) -> AttenderState:
    """Args:
         sent: the encoder states, aka keys and values. Usually but not necessarily an :class:`expression_seqs.ExpressionSequence`
    """
    raise NotImplementedError('init_sent must be implemented for Attender subclasses')

  def calc_attention(self, dec_state: dy.Expression, att_state: AttenderState = None) -> dy.Expression:
    """ Compute attention weights.

    Args:
      dec_state: the current decoder state, aka query, for which to compute the weights.
      att_state: the current attender state
    Returns:
      DyNet expression containing normalized attention scores
    """
    raise NotImplementedError('calc_attention must be implemented for Attender subclasses')

  def calc_context(self, dec_state: dy.Expression, att_state: AttenderState = None, attention: dy.Expression = None) -> dy.Expression:
    """ Compute weighted sum.

    Args:
      dec_state: the current decoder state, aka query, for which to compute the weighted sum.
      att_state: the current attender state
      attention: the attention vector to use. if not given it is calculated from the state(s).
    """
    attention = attention or self.calc_attention(dec_state, att_state)
    I = self.curr_sent.as_tensor()
    return I * attention

  def update(self, att_state: AttenderState, attention: dy.Expression):
    return None

class MlpAttender(Attender, Serializable):
  """
  Implements the attention model of Bahdanau et. al (2014)

  Args:
    input_dim: input dimension
    state_dim: dimension of dec_state inputs
    hidden_dim: hidden MLP dimension
    param_init: how to initialize weight matrices
    bias_init: how to initialize bias vectors
    truncate_dec_batches: whether the decoder drops batch elements as soon as these are masked at some time step.
  """

  yaml_tag = '!MlpAttender'


  @serializable_init
  def __init__(self,
               input_dim: numbers.Integral = Ref("exp_global.default_layer_dim"),
               state_dim: numbers.Integral = Ref("exp_global.default_layer_dim"),
               hidden_dim: numbers.Integral = Ref("exp_global.default_layer_dim"),
               param_init: param_initializers.ParamInitializer = Ref("exp_global.param_init", default=bare(param_initializers.GlorotInitializer)),
               bias_init: param_initializers.ParamInitializer = Ref("exp_global.bias_init", default=bare(param_initializers.ZeroInitializer)),
               truncate_dec_batches: bool = Ref("exp_global.truncate_dec_batches", default=False)) -> None:
    self.input_dim = input_dim
    self.state_dim = state_dim
    self.hidden_dim = hidden_dim
    self.truncate_dec_batches = truncate_dec_batches
    param_collection = param_collections.ParamManager.my_params(self)
    self.W = param_collection.add_parameters((hidden_dim, input_dim), init=param_init.initializer((hidden_dim, input_dim)))
    self.V = param_collection.add_parameters((hidden_dim, state_dim), init=param_init.initializer((hidden_dim, state_dim)))
    self.b = param_collection.add_parameters((hidden_dim,), init=bias_init.initializer((hidden_dim,)))
    self.U = param_collection.add_parameters((1, hidden_dim), init=param_init.initializer((1, hidden_dim)))
    self.curr_sent = None

  def init_sent(self, sent: expression_seqs.ExpressionSequence) -> None:
    self.attention_vecs = []
    self.curr_sent = sent
    I = self.curr_sent.as_tensor()
    self.WI = dy.affine_transform([self.b, self.W, I])
    wi_dim = self.WI.dim()
    # TODO(philip30): dynet affine transform bug, should be fixed upstream
    # if the input size is "1" then the last dimension will be dropped.
    if len(wi_dim[0]) == 1:
      self.WI = dy.reshape(self.WI, (wi_dim[0][0], 1), batch_size=wi_dim[1])
    return None

  def calc_attention(self, dec_state: dy.Expression, att_state: AttenderState = None) -> dy.Expression:
    WI = self.WI
    curr_sent_mask = self.curr_sent.mask
    if self.truncate_dec_batches:
      if curr_sent_mask:
        dec_state, WI, curr_sent_mask = batchers.truncate_batches(dec_state, WI, curr_sent_mask)
      else:
        dec_state, WI = batchers.truncate_batches(dec_state, WI)
    h = dy.tanh(dy.colwise_add(WI, self.V * dec_state))
    scores = dy.transpose(self.U * h)
    if curr_sent_mask is not None:
      scores = curr_sent_mask.add_to_tensor_expr(scores, multiplicator = -100.0)
    normalized = dy.softmax(scores)
    self.attention_vecs.append(normalized)
    return normalized

class DotAttender(Attender, Serializable):
  """
  Implements dot product attention of https://arxiv.org/abs/1508.04025
  Also (optionally) perform scaling of https://arxiv.org/abs/1706.03762

  Args:
    scale: whether to perform scaling
    truncate_dec_batches: currently unsupported
  """

  yaml_tag = '!DotAttender'

  @serializable_init
  def __init__(self,
               scale: bool = True,
               truncate_dec_batches: bool = Ref("exp_global.truncate_dec_batches", default=False)) -> None:
    if truncate_dec_batches: raise NotImplementedError("truncate_dec_batches not yet implemented for DotAttender")
    self.curr_sent = None
    self.scale = scale
    self.attention_vecs = []

  def init_sent(self, sent: expression_seqs.ExpressionSequence) -> None:
    self.curr_sent = sent
    self.attention_vecs = []
    self.I = dy.transpose(self.curr_sent.as_tensor())
    return None

  def calc_attention(self, dec_state: dy.Expression, att_state: AttenderState = None) -> dy.Expression:
    scores = self.I * dec_state
    if self.scale:
      scores /= math.sqrt(dec_state.dim()[0][0])
    if self.curr_sent.mask is not None:
      scores = self.curr_sent.mask.add_to_tensor_expr(scores, multiplicator = -100.0)
    normalized = dy.softmax(scores)
    self.attention_vecs.append(normalized)
    return normalized

class BilinearAttender(Attender, Serializable):
  """
  Implements a bilinear attention, equivalent to the 'general' linear
  attention of https://arxiv.org/abs/1508.04025

  Args:
    input_dim: input dimension; if None, use exp_global.default_layer_dim
    state_dim: dimension of dec_state inputs; if None, use exp_global.default_layer_dim
    param_init: how to initialize weight matrices; if None, use ``exp_global.param_init``
    truncate_dec_batches: currently unsupported
  """

  yaml_tag = '!BilinearAttender'

  @serializable_init
  def __init__(self,
               input_dim: numbers.Integral = Ref("exp_global.default_layer_dim"),
               state_dim: numbers.Integral = Ref("exp_global.default_layer_dim"),
               param_init: param_initializers.ParamInitializer = Ref("exp_global.param_init", default=bare(param_initializers.GlorotInitializer)),
               truncate_dec_batches: bool = Ref("exp_global.truncate_dec_batches", default=False)) -> None:
    if truncate_dec_batches: raise NotImplementedError("truncate_dec_batches not yet implemented for BilinearAttender")
    self.input_dim = input_dim
    self.state_dim = state_dim
    param_collection = param_collections.ParamManager.my_params(self)
    self.Wa = param_collection.add_parameters((input_dim, state_dim), init=param_init.initializer((input_dim, state_dim)))
    self.curr_sent = None

  def init_sent(self, sent: expression_seqs.ExpressionSequence) -> None:
    self.curr_sent = sent
    self.attention_vecs = []
    self.I = self.curr_sent.as_tensor()
    return None

  # TODO(philip30): Please apply masking here
  def calc_attention(self, dec_state: dy.Expression, att_state: AttenderState = None) -> dy.Expression:
    logger.warning("BilinearAttender does currently not do masking, which may harm training results.")
    scores = (dy.transpose(dec_state) * self.Wa) * self.I
    normalized = dy.softmax(scores)
    self.attention_vecs.append(normalized)
    return dy.transpose(normalized)

class LatticeBiasedMlpAttender(MlpAttender, Serializable):
  """
  Modified MLP attention, where lattices are assumed as input and the attention is biased toward confident nodes.

  Args:
    input_dim: input dimension
    state_dim: dimension of dec_state inputs
    hidden_dim: hidden MLP dimension
    param_init: how to initialize weight matrices
    bias_init: how to initialize bias vectors
    truncate_dec_batches: whether the decoder drops batch elements as soon as these are masked at some time step.
  """

  yaml_tag = '!LatticeBiasedMlpAttender'

  @events.register_xnmt_handler
  @serializable_init
  def __init__(self,
               input_dim: numbers.Integral = Ref("exp_global.default_layer_dim"),
               state_dim: numbers.Integral = Ref("exp_global.default_layer_dim"),
               hidden_dim: numbers.Integral = Ref("exp_global.default_layer_dim"),
               param_init: param_initializers.ParamInitializer = Ref("exp_global.param_init", default=bare(param_initializers.GlorotInitializer)),
               bias_init: param_initializers.ParamInitializer = Ref("exp_global.bias_init", default=bare(param_initializers.ZeroInitializer)),
               truncate_dec_batches: bool = Ref("exp_global.truncate_dec_batches", default=False)) -> None:
    super().__init__(input_dim=input_dim, state_dim=state_dim, hidden_dim=hidden_dim, param_init=param_init,
                     bias_init=bias_init, truncate_dec_batches=truncate_dec_batches)

  @events.handle_xnmt_event
  def on_start_sent(self, src):
    self.cur_sent_bias = np.full((src.sent_len(), 1, src.batch_size()), -1e10)
    for batch_i, lattice_batch_elem in enumerate(src):
      for node_i, node in enumerate(lattice_batch_elem.nodes):
        self.cur_sent_bias[node_i, 0, batch_i] = node.marginal_log_prob
    self.cur_sent_bias_expr = None

  def calc_attention(self, dec_state: dy.Expression, att_state: AttenderState = None) -> dy.Expression:
    WI = self.WI
    curr_sent_mask = self.curr_sent.mask
    if self.truncate_dec_batches:
      if curr_sent_mask:
        dec_state, WI, curr_sent_mask = batchers.truncate_batches(dec_state, WI, curr_sent_mask)
      else:
        dec_state, WI = batchers.truncate_batches(dec_state, WI)
    h = dy.tanh(dy.colwise_add(WI, self.V * dec_state))
    scores = dy.transpose(self.U * h)
    if curr_sent_mask is not None:
      scores = curr_sent_mask.add_to_tensor_expr(scores, multiplicator = -1e10)
    if self.cur_sent_bias_expr is None: self.cur_sent_bias_expr = dy.inputTensor(self.cur_sent_bias, batched=True)
    normalized = dy.softmax(scores + self.cur_sent_bias_expr)
    self.attention_vecs.append(normalized)
    return normalized

