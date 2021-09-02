# Copyright 2021 The TensorFlow Authors. All Rights Reserved.
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

"""Bounding Box List operations."""

import tensorflow as tf

from official.vision.beta.projects.centernet.ops import box_list
from official.vision.beta.projects.centernet.ops import loss_ops


def _copy_extra_fields(boxlist_to_copy_to, boxlist_to_copy_from):
  """Copies the extra fields of boxlist_to_copy_from to boxlist_to_copy_to.

  Args:
    boxlist_to_copy_to: BoxList to which extra fields are copied.
    boxlist_to_copy_from: BoxList from which fields are copied.

  Returns:
    boxlist_to_copy_to with extra fields.
  """
  for field in boxlist_to_copy_from.get_extra_fields():
    boxlist_to_copy_to.add_field(field, boxlist_to_copy_from.get_field(field))
  return boxlist_to_copy_to


def scale(boxlist, y_scale, x_scale):
  """scale box coordinates in x and y dimensions.

  Args:
    boxlist: BoxList holding N boxes
    y_scale: (float) scalar tensor
    x_scale: (float) scalar tensor
    scope: name scope.

  Returns:
    boxlist: BoxList holding N boxes
  """
  with tf.name_scope('Scale'):
    y_scale = tf.cast(y_scale, tf.float32)
    x_scale = tf.cast(x_scale, tf.float32)
    y_min, x_min, y_max, x_max = tf.split(
        value=boxlist.get(), num_or_size_splits=4, axis=1)
    y_min = y_scale * y_min
    y_max = y_scale * y_max
    x_min = x_scale * x_min
    x_max = x_scale * x_max
    scaled_boxlist = box_list.BoxList(
        tf.concat([y_min, x_min, y_max, x_max], 1))
    return _copy_extra_fields(scaled_boxlist, boxlist)


def area(boxlist):
  """Computes area of boxes.

  Args:
    boxlist: BoxList holding N boxes
    scope: name scope.

  Returns:
    a tensor with shape [N] representing box areas.
  """
  with tf.name_scope('Area'):
    y_min, x_min, y_max, x_max = tf.split(
        value=boxlist.get(), num_or_size_splits=4, axis=1)
    return tf.squeeze((y_max - y_min) * (x_max - x_min), [1])


def change_coordinate_frame(boxlist, window):
  """Change coordinate frame of the boxlist to be relative to window's frame.

  Given a window of the form [ymin, xmin, ymax, xmax],
  changes bounding box coordinates from boxlist to be relative to this window
  (e.g., the min corner maps to (0,0) and the max corner maps to (1,1)).

  An example use case is data augmentation: where we are given groundtruth
  boxes (boxlist) and would like to randomly crop the image to some
  window (window). In this case we need to change the coordinate frame of
  each groundtruth box to be relative to this new window.

  Args:
    boxlist: A BoxList object holding N boxes.
    window: A rank 1 tensor [4].

  Returns:
    Returns a BoxList object with N boxes.
  """
  with tf.name_scope('ChangeCoordinateFrame'):
    win_height = window[2] - window[0]
    win_width = window[3] - window[1]
    boxlist_new = scale(box_list.BoxList(
        boxlist.get() - [window[0], window[1], window[0], window[1]]),
        1.0 / win_height, 1.0 / win_width)
    boxlist_new = _copy_extra_fields(boxlist_new, boxlist)
    return boxlist_new


def matmul_gather_on_zeroth_axis(params, indices):
  """Matrix multiplication based implementation of tf.gather on zeroth axis.

  TODO(rathodv, jonathanhuang): enable sparse matmul option.

  Args:
    params: A float32 Tensor. The tensor from which to gather values.
      Must be at least rank 1.
    indices: A Tensor. Must be one of the following types: int32, int64.
      Must be in range [0, params.shape[0])
    scope: A name for the operation (optional).

  Returns:
    A Tensor. Has the same type as params. Values from params gathered
    from indices given by indices, with shape indices.shape + params.shape[1:].
  """
  with tf.name_scope('MatMulGather'):
    params_shape = loss_ops.combined_static_and_dynamic_shape(params)
    indices_shape = loss_ops.combined_static_and_dynamic_shape(indices)
    params2d = tf.reshape(params, [params_shape[0], -1])
    indicator_matrix = tf.one_hot(indices, params_shape[0])
    gathered_result_flattened = tf.matmul(indicator_matrix, params2d)
    return tf.reshape(gathered_result_flattened,
                      tf.stack(indices_shape + params_shape[1:]))


def gather(boxlist, indices, fields=None, use_static_shapes=False):
  """Gather boxes from BoxList according to indices and return new BoxList.

  By default, `gather` returns boxes corresponding to the input index list, as
  well as all additional fields stored in the boxlist (indexing into the
  first dimension).  However one can optionally only gather from a
  subset of fields.

  Args:
    boxlist: BoxList holding N boxes
    indices: a rank-1 tensor of type int32 / int64
    fields: (optional) list of fields to also gather from.  If None (default),
      all fields are gathered from.  Pass an empty fields list to only gather
      the box coordinates.
    scope: name scope.
    use_static_shapes: Whether to use an implementation with static shape
      gurantees.

  Returns:
    subboxlist: a BoxList corresponding to the subset of the input BoxList
    specified by indices
  Raises:
    ValueError: if specified field is not contained in boxlist or if the
      indices are not of type int32
  """
  with tf.name_scope('Gather'):
    if len(indices.shape.as_list()) != 1:
      raise ValueError('indices should have rank 1')
    if indices.dtype != tf.int32 and indices.dtype != tf.int64:
      raise ValueError('indices should be an int32 / int64 tensor')
    gather_op = tf.gather
    if use_static_shapes:
      gather_op = matmul_gather_on_zeroth_axis
    subboxlist = box_list.BoxList(gather_op(boxlist.get(), indices))
    if fields is None:
      fields = boxlist.get_extra_fields()
    fields += ['boxes']
    for field in fields:
      if not boxlist.has_field(field):
        raise ValueError('boxlist must contain all specified fields')
      subfieldlist = gather_op(boxlist.get_field(field), indices)
      subboxlist.add_field(field, subfieldlist)
    return subboxlist


def prune_completely_outside_window(boxlist, window):
  """Prunes bounding boxes that fall completely outside of the given window.

  The function clip_to_window prunes bounding boxes that fall
  completely outside the window, but also clips any bounding boxes that
  partially overflow. This function does not clip partially overflowing boxes.

  Args:
    boxlist: a BoxList holding M_in boxes.
    window: a float tensor of shape [4] representing [ymin, xmin, ymax, xmax]
      of the window
    scope: name scope.

  Returns:
    pruned_boxlist: a new BoxList with all bounding boxes partially or fully in
      the window.
    valid_indices: a tensor with shape [M_out] indexing the valid bounding boxes
     in the input tensor.
  """
  with tf.name_scope('PruneCompleteleyOutsideWindow'):
    y_min, x_min, y_max, x_max = tf.split(
        value=boxlist.get(), num_or_size_splits=4, axis=1)
    win_y_min, win_x_min, win_y_max, win_x_max = tf.unstack(window)
    coordinate_violations = tf.concat([
        tf.greater_equal(y_min, win_y_max), tf.greater_equal(x_min, win_x_max),
        tf.less_equal(y_max, win_y_min), tf.less_equal(x_max, win_x_min)
    ], 1)
    valid_indices = tf.reshape(
        tf.where(tf.logical_not(tf.reduce_any(coordinate_violations, 1))), [-1])
    return gather(boxlist, valid_indices), valid_indices


def clip_to_window(boxlist, window, filter_nonoverlapping=True):
  """Clip bounding boxes to a window.

  This op clips any input bounding boxes (represented by bounding box
  corners) to a window, optionally filtering out boxes that do not
  overlap at all with the window.

  Args:
    boxlist: BoxList holding M_in boxes
    window: a tensor of shape [4] representing the [y_min, x_min, y_max, x_max]
      window to which the op should clip boxes.
    filter_nonoverlapping: whether to filter out boxes that do not overlap at
      all with the window.
    scope: name scope.

  Returns:
    a BoxList holding M_out boxes where M_out <= M_in
  """
  with tf.name_scope('ClipToWindow'):
    y_min, x_min, y_max, x_max = tf.split(
        value=boxlist.get(), num_or_size_splits=4, axis=1)
    win_y_min = window[0]
    win_x_min = window[1]
    win_y_max = window[2]
    win_x_max = window[3]
    y_min_clipped = tf.maximum(tf.minimum(y_min, win_y_max), win_y_min)
    y_max_clipped = tf.maximum(tf.minimum(y_max, win_y_max), win_y_min)
    x_min_clipped = tf.maximum(tf.minimum(x_min, win_x_max), win_x_min)
    x_max_clipped = tf.maximum(tf.minimum(x_max, win_x_max), win_x_min)
    clipped = box_list.BoxList(
        tf.concat([y_min_clipped, x_min_clipped, y_max_clipped, x_max_clipped],
                  1))
    clipped = _copy_extra_fields(clipped, boxlist)
    if filter_nonoverlapping:
      areas = area(clipped)
      nonzero_area_indices = tf.cast(
          tf.reshape(tf.where(tf.greater(areas, 0.0)), [-1]), tf.int32)
      clipped = gather(clipped, nonzero_area_indices)
    return clipped
