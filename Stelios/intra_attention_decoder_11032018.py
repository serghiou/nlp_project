"""intra_attention_decoder.py"""
# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
# Modifications Copyright 2017 Abigail See
# Modifications Copyright 2018 Stelios Serghiou, Peter Li, Apurva Pancholi

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
# ==============================================================================

"""This file defines the decoder"""

import tensorflow as tf
import argparse
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import variable_scope


# Note: this function is based attention_decoder
# In the future, it would make more sense to write variants on the attention mechanism using the new seq2seq library for tensorflow 1.0: https://www.tensorflow.org/api_guides/python/contrib.seq2seq#Attention
def intra_attention_decoder(decoder_inputs, initial_state, encoder_states,
                            enc_padding_mask, cell,
                            initial_state_attention=False, pointer_gen=True,
                            use_coverage=False, prev_coverage=None):
    """
    Args:
      decoder_inputs: A list of 2D Tensors [batch_size x input_size].
      initial_state: 2D Tensor [batch_size x cell.state_size].
      encoder_states: 3D Tensor [batch_size x attn_length x attn_size].
      enc_padding_mask: 2D Tensor [batch_size x attn_length] containing 1s and 0s; indicates which of the encoder locations are padding (0) or a real token (1).
      cell: rnn_cell.RNNCell defining the cell function and size.
      initial_state_attention:
        Note that this attention decoder passes each decoder input through a linear layer with the previous step's context vector to get a modified version of the input. If initial_state_attention is False, on the first decoder step the "previous context vector" is just a zero vector. If initial_state_attention is True, we use initial_state to (re)calculate the previous step's context vector. We set this to False for train/eval mode (because we call attention_decoder once for all decoder steps) and True for decode mode (because we call attention_decoder once for each decoder step).
      pointer_gen: boolean. If True, calculate the generation probability p_gen for each decoder step.
      use_coverage: boolean. If True, use coverage mechanism.
      prev_coverage:
        If not None, a tensor with shape (batch_size, attn_length). The previous step's coverage vector. This is only not None in decode mode when using coverage.

    Returns:
      outputs: A list of the same length as decoder_inputs of 2D Tensors of
        shape [batch_size x cell.output_size]. The output vectors.
      state: The final state of the decoder. A tensor shape [batch_size x cell.state_size].
      attn_dists: A list containing tensors of shape (batch_size,attn_length).
        The attention distributions for each decoder step.
      p_gens: List of scalars. The values of p_gen for each decoder step. Empty list if pointer_gen=False.
      coverage: Coverage vector on the last step computed. None if use_coverage=False.
    """
    with variable_scope.variable_scope("attention_decoder") as scope:
        # if this line fails, it's because the batch size isn't defined
        batch_size = encoder_states.get_shape()[0].value
        # if this line fails, it's because the attention length isn't defined
        attn_size = encoder_states.get_shape()[2].value

        # Reshape encoder_states (need to insert a dim)
        # actual shape of encoder_states.get_shape (16, ?, 1, 512)
        # now is shape (batch_size, attn_len, 1, attn_size)
        encoder_states = tf.expand_dims(encoder_states, axis=2)

        def intra_temporal_attention(decoder_states):
            '''
            Get Intra-Temporal Attention Score. Refs to original paper section 2.1 https://arxiv.org/abs/1705.04304
            :param decoder_state:
            :param coverage: None
            :return:attention score
            '''
            # Extract hidden state from list and tuple of decoder states
            decoder_state = decoder_states[-1][1]
            # decoder_state[1].get_shape() (batch_size, hidden_vec_size)

            decoder_hidden_vec_size = decoder_state.get_shape()[1].value
            encoder_hidden_vec_size = encoder_states.get_shape()[3].value

            # Intra-Temporal Attention
            with variable_scope.variable_scope("IT_Attention"):

                # Equation (2) W_e_attn for h_d (hidden decoder vectors) and
                # h_e (hidden encoder vectors)
                W_e_attn = tf.get_variable('W_e_attn',
                                           shape=(1, 1,
                                                  encoder_hidden_vec_size,
                                                  decoder_hidden_vec_size),
                        initializer=tf.contrib.layers.xavier_initializer())

                decoder_T = len(decoder_states)

                encoder_states_dot_W = nn_ops.conv2d(encoder_states, W_e_attn,
                                                    [1, 1, 1, 1],
                                                    "SAME")
                # shape (batch_size,?,1,decoder_hidden_vec_size)

                # tf.logging.info("encoder_states_dot_W.shape {}".format(encoder_states_dot_W.get_shape()))
                # encoder_states_dot_W.shape (16, len_attn, 1, 256)

                decoder_state = tf.expand_dims(
                    tf.expand_dims(decoder_state, 1), 1)
                    # reshape to (batch_size, 1, 1, decoder_hidden_vec_size)

                e = math_ops.reduce_sum(
                    decoder_state * encoder_states_dot_W, [2, 3])
                # shape: (batch_size x attn_length)

                # Equation (3)
                if decoder_T == 1:
                    e_prime = tf.exp(e)
                else:
                    denominator = tf.reduce_sum(tf.exp(eti), axis=0)
                    e_prime = tf.divide(tf.exp(e), denominator)
                # tf.logging.info("e_prime.shape:{}".format(e_prime.get_shape())) # (batch_size, attn_length)

                # append to eti list after e_prime been calculated
                eti.append(e)
                # tf.logging.info("e.shape:{}".format(e.get_shape())) # e.shape:(batch_size, ?)

                # Equation (4)
                attn_score = tf.nn.softmax(e_prime)
                # tf.logging.info("attn_score.shape:{}".format(attn_score.get_shape())) # attn_score.shape:(16, attn_length)

                return attn_score

        def hybrid_attention(decoder_states, coverage=None):
            '''
            The hybrid attention model which concat Intra Temporal Attention and Intra-Decoder Attention to get context and distrubution
            Args:
                decoder_states: list of decoder hidden states shape,
                    size = list([batch_size, hidden_dim])
                coverage: initialized to None or a previous coverage tensor
            Returns:
                context vector: tensor of weighed encoder hidden states,
                    size = [batch_size x hidden_dims]
                attn_dist: context vector reweighted to account for masking
                    size = [batch_size x hidden_dims]
                decoder context: tensor of weighted decoder hidden states,
                    size = [batch_size x hidden_dims]
                coverage: as per Abi's code to prevent repetition
            '''
            temporal_attention = intra_temporal_attention(decoder_states)
            decoder_attention = intra_decoder_attention(decoder_states_stack)

            # Calculate encoder distribution
            # Mask padded sequences
            attn_dist = masked_attention(temporal_attention, enc_padding_mask)
            # Equation (5)
            # encoder_states: batch_size x attn_length x 1 x attn_size
            # attn_dist: batch_size x attn_length
            # Result has shape (batch_size, 1, encoder_hidden_size)
            # --> After squeeze (batch_size, encoder_hidden_size)
            temporal_context = tf.squeeze(
                tf.einsum('btkh,bt->bkh', encoder_states, attn_dist))
            # print(temporal_context.get_shape())--> (16, 512)
            context_vector = temporal_context

            # Equation (8)
            # decoder_states_stack: T x batch_size x decoder_hidden_size
            # decoder_attention: batch_size x T - 1
            # Result has shape (batch_size, decoder_hidden_size)
            if len(decoder_states) > 1:
                decoder_context = tf.einsum('tbh,bt->bh',
                                            decoder_states_stack[:-1, :, :],
                                            decoder_attention)
                                            # ignore the last e
            else:
                decoder_context = tf.zeros(
                    shape=[decoder_attention.get_shape().as_list()[0],
                    decoder_states[-1][1].get_shape().as_list()[1]])

            return context_vector, attn_dist, decoder_context, coverage

        # USING ATTENTION
        tf.logging.info("Using Intra Temporal + Decoder Attention Model")

        # The eti in equation (1), eti is a list length of decoder_steps_length, and each eti[t] is a list of length encoder_steps_length
        eti = []  # eti and ett does NOT share same weight
        outputs = []  # stores decoder hidden state outputs
        attn_dists = []
        p_gens = []  # probabilities for pointer generator model of Abi
        decoder_states = []  # hidden states from each decoder step
        temporal_attention_scores = []
        input_contexts = []  # encoder weighted hidden states by attention
        decoder_contexts = []  # decoder weighted hidden states by attention
        state = initial_state  # state to be fed into the first decoder step
        # don't need initial_state for caculation
        # decoder_states.append(state)
        coverage = prev_coverage  # initialize to None or specific value
        context_vector = array_ops.zeros([batch_size, attn_size])
        # Ensure the second shape of attention vectors is set.
        context_vector.set_shape([None, attn_size])

        if initial_state_attention:  # true in decode mode
            decoder_states_stack = tf.stack([[initial_state]])
            # Re-calculate the context vector from the previous step so that we can pass it through a linear layer with this step's input to get a modified version of the input
            context_vector, _, decoder_context, coverage = hybrid_attention(
                [initial_state], coverage)
            # in decode mode, this is what updates the coverage vector

        for i, inp in enumerate(decoder_inputs):
            tf.logging.info("Adding attention_decoder timestep %i of %i", i,
                            len(decoder_inputs))

            if i > 0:
                variable_scope.get_variable_scope().reuse_variables()

            # Merge input and previous attentions into one vector x of the same
            # size as inp
            input_size = inp.get_shape().with_rank(2)[1]
            if input_size.value is None:
                raise ValueError(
                    "Could not infer input size from input: %s" % inp.name)
            x = linear([inp] + [context_vector], input_size, True)

            # Run the decoder RNN cell. cell_output = decoder state
            cell_output, state = cell(x, state)

            # Keep the decoder states
            decoder_states.append(state)
            _, decoder_states_list = map(list, zip(*decoder_states))
            decoder_states_stack = tf.stack(decoder_states_list)
            # print(decoder_states_stack.get_shape()) #(T,batch_size, decoder_hidden_size)

            # Run the attention mechanism
            if i == 0 and initial_state_attention:  # always true in decode mode
                with variable_scope.variable_scope(
                        variable_scope.get_variable_scope(), reuse=True):
                    # you need this because you've already run the initial attention(...) call

                    context_vector, attn_dist, decoder_context, _ = hybrid_attention(decoder_states, coverage)
                        # don't allow coverage to update
            else:
                context_vector, attn_dist, decoder_context, coverage = hybrid_attention(decoder_states, coverage)

            attn_dists.append(attn_dist)
            temporal_attention_scores.append(attn_dist)
            input_contexts.append(context_vector)
            decoder_contexts.append(decoder_context)

            # Calculate p_gen
            if pointer_gen:
                with tf.variable_scope('calculate_pgen'):
                    p_gen = linear(
                        [context_vector, state.c, state.h, x], 1, True)  # a scalar
                    p_gen = tf.sigmoid(p_gen)
                    p_gens.append(p_gen)

            # Append hidden states
            outputs.append(cell_output)

        # If using coverage, reshape it
        if coverage is not None:
            coverage = array_ops.reshape(coverage, [batch_size, -1])

        # Common part of return
        decoder_rets = {"outputs": outputs, "state": state,
                        "attn_dists": attn_dists, "p_gens": p_gens,
                        "coverage": coverage}
        # Extra returns for Socher model
        decoder_rets["temporal_attention_scores"] = temporal_attention_scores
        decoder_rets["input_contexts"] = input_contexts
        decoder_rets["decoder_contexts"] = decoder_contexts

        return decoder_rets


def intra_decoder_attention(decoder_states_stack):
    '''
    Get Intra-Decoder Attention Score. Refs to original paper section 2.2
    https://arxiv.org/abs/1705.04304.
    Args:
        decoder_states_stack: tensor of decoder hidden states
            size: [T, batch_size, decoder_hidden_size]
    Returns:
        attn_score: tensor of attnetion scores alpha_d_tt (Equation 7),
            size: [batch_size, T - 1]
    '''
    batch_size = decoder_states_stack[-1].get_shape()[0].value
    decoder_T = decoder_states_stack.get_shape()[0]
    decoder_state = decoder_states_stack[-1]
    # decoder_state[1].get_shape() (batch_size, hidden_vec_size)

    decoder_hidden_vec_size = decoder_state.get_shape().as_list()[-1]

    # Intra-Decoder Attention
    with variable_scope.variable_scope("ID_Attention"):

        # W_d_attn of Equation 6
        W_d_attn = tf.get_variable('W_d_attn',
            shape=(decoder_hidden_vec_size, decoder_hidden_vec_size),
            initializer=tf.contrib.layers.xavier_initializer())

        if decoder_T > 1:
            # Equation (6)
            # return shape [T-1, batch_size, hidden_state_size]
            decoder_states_dot_W = tf.einsum(
                "ij,tbi->tbj", W_d_attn, decoder_states_stack[:-1])

            e = tf.einsum("tbi,bi->bt", decoder_states_dot_W, decoder_state)
            # return shape [batch_size, T-1]

            # Equation (7)
            attn_score = tf.nn.softmax(e)
            # shape (batch_size, decoder_T-1)
        else:
            attn_score = tf.zeros([batch_size, 1])

        return attn_score


def masked_attention(e, enc_padding_mask):
    '''
    Apply enc_padding_mask on encoder attention, and re-normalized it
    Args:
        e: tensor of original encoder attention scores
            size = batch_size x attn_length
        enc_padding_mask: tensor of masks
            size = batch_size x attn_length
    Returns:
        attn_dist: masked attention score,
            size = batch_size x attn_length
    '''
    attn_dist = e
    attn_dist *= enc_padding_mask  # apply mask
    masked_sums = tf.reduce_sum(attn_dist, axis=1)  # shape (batch_size)
    return attn_dist / tf.reshape(masked_sums, [-1, 1])  # re-normalize


def linear(args, output_size, bias, bias_start=0.0, scope=None):
    """Linear map: sum_i(args[i] * W[i]), where W[i] is a variable.
    Args:
      args: a 2D Tensor or a list of 2D, batch x n, Tensors.
      output_size: int, second dimension of W[i].
      bias: boolean, whether to add a bias term or not.
      bias_start: starting value to initialize the bias; 0 by default.
      scope: VariableScope for the created subgraph; defaults to "Linear".
    Returns:
      A 2D Tensor with shape [batch x output_size] equal to
      sum_i(args[i] * W[i]), where W[i]s are newly created matrices.
    Raises:
      ValueError: if some of the arguments has unspecified or wrong shape.
    """
    if args is None or (isinstance(args, (list, tuple)) and not args):
        raise ValueError("`args` must be specified")
    if not isinstance(args, (list, tuple)):
        args = [args]

    # Calculate the total size of arguments on dimension 1.
    total_arg_size = 0
    shapes = [a.get_shape().as_list() for a in args]
    for shape in shapes:
        if len(shape) != 2:
            raise ValueError(
                "Linear is expecting 2D arguments: %s" % str(shapes))
        if not shape[1]:
            raise ValueError(
                "Linear expects shape[1] of arguments: %s" % str(shapes))
        else:
            total_arg_size += shape[1]

    # tf.logging.info("Linear Matrix [total_arg_size:{},output_size:{}]".format(total_arg_size, output_size))
    '''
    INFO:tensorflow:Linear Matrix [total_arg_size:640,output_size:128]
    INFO:tensorflow:Linear Matrix [total_arg_size:512,output_size:512]
    '''
    # Now the computation.
    with tf.variable_scope(scope or "Linear"):
        matrix = tf.get_variable("Matrix", [total_arg_size, output_size])
        if len(args) == 1:
            res = tf.matmul(args[0], matrix)
        else:
            res = tf.matmul(tf.concat(axis=1, values=args), matrix)
        if not bias:
            return res
        bias_term = tf.get_variable(
            "Bias", [output_size], initializer=tf.constant_initializer(bias_start))
    return res + bias_term

############ The unit tests ###############


def test_intra_decoder_attention(args):
    #(decoder_states, decoder_states_stack):
    '''
    decoder_states - list(Tensor)
    :param args:
    :return:
    '''
    batch_size = 5
    max_total_time = 4
    input_vector_size = 3
    hidden_vector_size = 2
    lstm_cell = tf.nn.rnn_cell.LSTMCell(hidden_vector_size)
    initial_state = lstm_cell.zero_state(batch_size, tf.float32)
    inputs = tf.random_normal(shape=(batch_size, input_vector_size))
    decoder_states = []
    for _ in range(max_total_time):
        _, hidden_state = lstm_cell(inputs, initial_state)
        decoder_states.append(hidden_state)

    _, decoder_states_list = map(list, zip(*decoder_states))
    decoder_states_stack = tf.stack(decoder_states_list)

    attn = intra_decoder_attention(decoder_states_stack)

    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        _attn = sess.run(attn)
        print(_attn)


'''
--first run result---
[[0.3333333  0.3333333  0.3333333  0.3333333 ]
 [0.33333334 0.33333334 0.33333334 0.33333334]
 [0.33333334 0.33333334 0.33333334 0.33333334]
 [0.3333333  0.3333333  0.3333333  0.3333333 ]
 [0.33333334 0.33333334 0.33333334 0.33333334]]
--new run result--
[[0.33333334 0.33333334 0.33333334]
 [0.33333334 0.33333334 0.33333334]
 [0.33333334 0.33333334 0.33333334]
 [0.33333334 0.33333334 0.33333334]
 [0.33333334 0.33333334 0.33333334]]
'''


def test_attention_mask(args):
    '''
    Unit test for attention mask
    '''
    batch_size = 4
    vector_size = 3
    old_attn = tf.random_normal(shape=[batch_size, vector_size])
    mask = tf.constant([1.0, 1.0, 0])
    new_attn = masked_attention(old_attn, mask)
    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        _new_attn, _mask, _old_attn = sess.run([new_attn, mask, old_attn])
        print("old_attn---")
        print(_old_attn)
        print("mask----")
        print(_mask)
        print("new_attn---")
        print(_new_attn)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Test tokenization for matching parameter dimensions')
    subparsers = parser.add_subparsers()

    command_parser = subparsers.add_parser(
        'test1', help='Test attention mask')
    command_parser.set_defaults(func=test_attention_mask)

    command_parser = subparsers.add_parser(
        'test2', help='test intra decoder attention')
    command_parser.set_defaults(func=test_intra_decoder_attention)

    ARGS = parser.parse_args()
    if not hasattr(ARGS, 'func'):
        parser.print_help()
    else:
ARGS.func(ARGS)
