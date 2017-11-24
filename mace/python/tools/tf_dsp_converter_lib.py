from mace.proto import mace_pb2
import tensorflow as tf
from operator import mul
from dsp_ops import DspOps
from mace.python.tools import graph_util

# converter --input ../libcv/quantized_icnet.pb --output quantized_icnet_dsp.pb \
# --runtime dsp --input_dim input_node,1,480,480,3 --output_node icnet/output_node

padding_mode = {
  'NA': 0,
  'SAME': 1,
  'VALID': 2,
  'MIRROR_REFLECT': 3,
  'MIRROR_SYMMETRIC': 4,
  'SAME_CAFFE': 5
}

def get_tensor_name_from_op(op_name, port):
  return op_name + ':' + str(port)

def get_node_from_map(op_map, op_or_tensor_name):
  op_name = op_or_tensor_name.split(':')[0]
  return op_map[op_name]

def get_op_and_port_from_tensor(tensor_name):
  op, port = tensor_name.split(':')
  port = int(port)
  return op, port

def max_elem_size(tensor):
  if len(tensor.shape.as_list()) == 0:
    return tensor.dtype.size
  else:
    return reduce(mul, tensor.shape.as_list()) * tensor.dtype.size

def find_dtype(tensor_dtype):
  if tensor_dtype == tf.float32:
    return mace_pb2.DT_FLOAT
  elif tensor_dtype == tf.uint8 or tensor_dtype == tf.quint8:
    return mace_pb2.DT_UINT8
  elif tensor_dtype == tf.int32 or tensor_dtype == tf.qint32:
    return mace_pb2.DT_INT32
  else:
    raise Exception('Unsupported data type: ', tensor_dtype)

def has_padding_and_strides(op):
  return 'padding' in op.node_def.attr and 'strides' in op.node_def.attr

def is_node_flatten_reshape(op):
  return op.type == 'Reshape' and len(op.outputs[0].shape) == 1

def get_input_tensor(op, index):
  input_tensor = op.inputs[index]
  if input_tensor.op.type == 'Reshape':
    input_tensor = get_input_tensor(input_tensor.op, 0)
  return input_tensor

def add_shape_const_node(net_def, op, values, name):
  print ('Add const node: ', op.name + '/' + name)
  tensor = net_def.tensors.add()
  node_name = op.name + '/' + name
  tensor.name = node_name + ':0'
  tensor.data_type =  mace_pb2.DT_INT32
  tensor.dims.extend(values)
  return tensor.name

def convert_ops(unresolved_ops, resolved_ops, net_def, output_node, dsp_ops):
  first_op = unresolved_ops[0]
  print ('Op: ', first_op.name, first_op.type, first_op.outputs[0].shape)

  if first_op.name in resolved_ops:
    pass

  elif first_op.type == 'Const':
    print ('Add const node: ', first_op.name)
    tf_tensor = first_op.outputs[0].eval()
    tensor = net_def.tensors.add()
    tensor.name = first_op.outputs[0].name
    tensor.data_type = find_dtype(first_op.outputs[0].dtype)
    shape = list(tf_tensor.shape)
    if len(shape) > 0:
      tensor.dims.extend(shape)
    if first_op.outputs[0].dtype == tf.float32:
      tensor.float_data.extend(tf_tensor.astype(float).flat)
    elif first_op.outputs[0].dtype == tf.int32 or \
            first_op.outputs[0].dtype == tf.int8 or \
            first_op.outputs[0].dtype == tf.int16 or \
            first_op.outputs[0].dtype == tf.quint8 or \
            first_op.outputs[0].dtype == tf.quint16:
      tensor.int32_data.extend(tf_tensor.astype(int).flat)

  else:
    op_def = net_def.op.add()
    op_def.name = first_op.name
    op_def.type = dsp_ops.map_nn_op(first_op.type)
    op_def.padding = padding_mode['NA']

    if len(first_op.outputs) > 0 and first_op.type == 'Dequantize' \
        and len(first_op.outputs[0].consumers()) > 0 \
        and (first_op.outputs[0].consumers()[0].type == 'SpaceToBatchND' \
        or first_op.outputs[0].consumers()[0].type == 'BatchToSpaceND'):
      input_tensor = first_op.inputs[0]
      min_tensor = first_op.inputs[1]
      max_tensor = first_op.inputs[2]
      s2b_op = first_op.outputs[0].consumers()[0]
      reshape_op = s2b_op.outputs[0].consumers()[0]
      min_op = reshape_op.outputs[0].consumers()[0]
      max_op = reshape_op.outputs[0].consumers()[1]
      quantize_op = min_op.outputs[0].consumers()[0]
      resolved_ops.add(s2b_op.name)
      resolved_ops.add(reshape_op.name)
      resolved_ops.add(min_op.name)
      resolved_ops.add(max_op.name)
      resolved_ops.add(quantize_op.name)

      op_def.name = quantize_op.name
      op_def.type = dsp_ops.map_nn_op('Quantized' + s2b_op.type)
      op_def.input.append(input_tensor.name)
      op_def.input.extend([t.name for t in s2b_op.inputs[1:]])
      op_def.input.extend([min_tensor.name, max_tensor.name])
      op_def.out_max_byte_size.extend([max_elem_size(out) for out in quantize_op.outputs])
    elif has_padding_and_strides(first_op):
      op_def.padding = padding_mode[first_op.get_attr('padding')]
      op_def.input.extend([t.name for t in first_op.inputs])
      if 'ksize' in first_op.node_def.attr:
        ksize = first_op.get_attr('ksize')
        ksize_tensor = add_shape_const_node(net_def, first_op, ksize, 'ksize')
        op_def.input.extend([ksize_tensor])
      strides = first_op.get_attr('strides')
      strides_tensor = add_shape_const_node(net_def, first_op, strides, 'strides')
      op_def.input.extend([strides_tensor])
      op_def.out_max_byte_size.extend([max_elem_size(out) for out in first_op.outputs])
    elif is_node_flatten_reshape(first_op):
      op_def.type = 'Flatten'
      op_def.input.extend([t.name for t in first_op.inputs])
      op_def.out_max_byte_size.extend([max_elem_size(out) for out in first_op.outputs])
    elif dsp_ops.has_op(first_op.type):
      op_def.input.extend([t.name for t in first_op.inputs])
      op_def.out_max_byte_size.extend([max_elem_size(out) for out in first_op.outputs])
    else:
      raise Exception('Unsupported op: ', first_op)

    resolved_ops.add(first_op.name)

  del unresolved_ops[0]

def add_output_node(net_def, output_node):
  op_def = net_def.op.add()
  op_def.name = '__output__'
  op_def.type = 'OUTPUT'
  op_def.input.extend([get_tensor_name_from_op(output_node, 0)])

def reverse_batch_to_space_and_biasadd(net_def):
  tensor_map = {}
  for tensor in net_def.tensors:
    tensor_map[tensor.name] = tensor
  op_map = {}
  for op in net_def.op:
    op_map[op.name] = op
  consumers = {}
  for op in net_def.op:
    for ipt in op.input:
      if ipt not in consumers:
        consumers[ipt] = []
      consumers[ipt].append(op)

  new_ops = []
  skip_ops = set()
  visited_ops = set()

  for op in net_def.op:
    if op.name in visited_ops:
      pass
    # pattern: QConv -> RR -> R -> QB2S -> QBiasAdd -> RR -> R
    success = False
    if op.type == 'Requantize_32to8':
      biasadd_requantize_op = op
      biasadd_op = get_node_from_map(op_map, biasadd_requantize_op.input[0])
      if biasadd_op.type == 'QuantizedBiasAdd_8p8to32':
        b2s_op = get_node_from_map(op_map, biasadd_op.input[0])
        if b2s_op.type == 'QuantizedBatchToSpaceND_8':
          conv_requantize_op = get_node_from_map(op_map, b2s_op.input[0])
          conv_op = get_node_from_map(op_map, conv_requantize_op.input[0])
          if conv_op.type == 'QuantizedConv2d_8x8to32':
            new_biasadd_op = mace_pb2.OperatorDef()
            new_biasadd_op.CopyFrom(biasadd_op)
            new_biasadd_op.input[0] = get_tensor_name_from_op(conv_requantize_op.name, 0)
            new_biasadd_op.input[2] = get_tensor_name_from_op(conv_requantize_op.name, 1)
            new_biasadd_op.input[3] = get_tensor_name_from_op(conv_requantize_op.name, 2)
            new_biasadd_op.out_max_byte_size[0] = conv_requantize_op.out_max_byte_size[0] * 4

            new_biasadd_requantize_op = mace_pb2.OperatorDef()
            new_biasadd_requantize_op.CopyFrom(biasadd_requantize_op)
            new_biasadd_requantize_op.out_max_byte_size[0] = new_biasadd_op.out_max_byte_size[0] / 4

            new_b2s_op = mace_pb2.OperatorDef()
            new_b2s_op.CopyFrom(b2s_op)
            new_b2s_op.input[0] = get_tensor_name_from_op(biasadd_requantize_op.name, 0)
            new_b2s_op.input[3] = get_tensor_name_from_op(biasadd_requantize_op.name, 1)
            new_b2s_op.input[4] = get_tensor_name_from_op(biasadd_requantize_op.name, 2)

            new_ops.extend([new_biasadd_op, new_biasadd_requantize_op, new_b2s_op])
            skip_ops = skip_ops.union([biasadd_op.name, biasadd_requantize_op.name, b2s_op.name])
            visited_ops.add(op.name)

            follow_ops = consumers[get_tensor_name_from_op(biasadd_requantize_op.name, 0)]
            for follow_op in follow_ops:
              new_follow_op = mace_pb2.OperatorDef()
              new_follow_op.CopyFrom(follow_op)
              for i in range(len(follow_op.input)):
                for k in range(3):
                  if new_follow_op.input[i] == get_tensor_name_from_op(biasadd_requantize_op.name, k):
                    new_follow_op.input[i] = get_tensor_name_from_op(b2s_op.name, k)
              new_ops.append(new_follow_op)
              skip_ops.add(follow_op.name)
              visited_ops.add(follow_op.name)

    visited_ops.add(op.name)

  new_net_def = mace_pb2.NetDef()
  new_net_def.tensors.extend(tensor_map.values())
  for op in net_def.op:
    if op.name not in skip_ops:
      new_net_def.op.extend([op])
  new_net_def.op.extend(new_ops)

  return new_net_def

def add_node_id(net_def):
  node_id_counter = 0
  node_id_map = {}
  for tensor in net_def.tensors:
    tensor.node_id = node_id_counter
    node_id_counter += 1
    tensor_op, port = get_op_and_port_from_tensor(tensor.name)
    node_id_map[tensor_op] = tensor.node_id

  for op in net_def.op:
    op.node_id = node_id_counter
    node_id_counter += 1
    node_id_map[op.name] = op.node_id
    for ipt in op.input:
      op_name, port = get_op_and_port_from_tensor(ipt)
      node_id = node_id_map[op_name]
      node_input = op.node_input.add()
      node_input.node_id = node_id
      node_input.output_port = int(port)

  return net_def

def add_input_output_info(net_def, input_node, output_node, graph):
  input_tensor = graph.get_tensor_by_name(get_tensor_name_from_op(input_node, 0))
  output_tensor = graph.get_tensor_by_name(get_tensor_name_from_op(output_node, 0))

  for op in net_def.op:
    if op.name == input_node:
      input_info = net_def.input_info.add()
      input_info.name = op.name
      input_info.node_id = op.node_id
      input_info.dims.extend(input_tensor.shape.as_list())
      input_info.max_byte_size = max_elem_size(input_tensor)
      input_info.data_type = find_dtype(input_tensor.dtype)
    elif op.name == output_node:
      output_info = net_def.output_info.add()
      output_info.name = op.name
      output_info.node_id = op.node_id
      output_info.dims.extend(output_tensor.shape.as_list())
      output_info.max_byte_size = max_elem_size(output_tensor)
      output_info.data_type = find_dtype(output_tensor.dtype)

  return net_def

def convert_to_mace_pb(input_graph_def, input_node, output_node):
  """
    nnlib does not have batch norm, so use tensorflow optimizer to fold
     batch norm with convolution. The fold optimization reorders ops, so
     we sort ops first by topology.
  """
  input_graph_def = graph_util.sort_tf_graph(input_graph_def)
  net_def = mace_pb2.NetDef()

  with tf.Session() as session:
    with session.graph.as_default() as graph:
      tf.import_graph_def(input_graph_def, name="")
      ops = graph.get_operations()
      dsp_ops = DspOps()
      resolved_ops = set()
      # convert const node
      unresolved_ops = [op for op in ops if op.type == 'Const']
      while len(unresolved_ops) > 0:
        convert_ops(unresolved_ops, resolved_ops, net_def, output_node, dsp_ops)

      # convert op node
      unresolved_ops = [op for op in ops if op.type != 'Const']
      while len(unresolved_ops) > 0:
        convert_ops(unresolved_ops, resolved_ops, net_def, output_node, dsp_ops)

      add_output_node(net_def, output_node)
      # optimized_net_def = reverse_batch_to_space_and_biasadd(net_def)
      sorted_net_def = graph_util.sort_mace_graph(net_def, '__output__')
      net_def_with_node_id = add_node_id(sorted_net_def)

      final_net_def = add_input_output_info(net_def_with_node_id, input_node, output_node, graph)

  return final_net_def
