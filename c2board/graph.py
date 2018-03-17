# X: write our own graph interface
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from builtins import bytes
import copy
import os
import re
import six

from caffe2.proto import caffe2_pb2
from caffe2.python import core, workspace

from c2board.src.graph_pb2 import GraphDef
from c2board.src.node_def_pb2 import NodeDef
from c2board.src.versions_pb2 import VersionDef
# X: need to add attribute values
from c2board.src.attr_value_pb2 import AttrValue
from c2board.src.tensor_shape_pb2 import TensorShapeProto


# X: it seems not necessary to propagate it..
def _propagate_device_option(net):
    if not net.HasField("device_option"):
        return
    for op in net.op:
        if not op.HasField("device_option"):
            op.device_option.CopyFrom(net.device_option)

# X: get blob names, not sure if needed
def _get_blob_names(ops):
    names = set()
    for op in ops:
        names.update(op.input)
        names.update(op.output)
    return {name: name for name in names}

# X: wow this is very tensorflow
def _make_unique_name(seen, name, min_version=0):
    assert name is not None
    i = min_version
    x = '%s_%d' % (name, i) if i else name
    while x in seen:
        i += 1
        x = '%s_%d' % (name, i)
    seen.add(x)
    return x

def _remap_keys(m, f):
    m2 = {f(key): value for key, value in six.iteritems(m)}
    m.clear()
    m.update(m2)

# X: rename all the ops
def _rename_all(track_blob_names, ops, f):
    seen = set()
    renamed = {}

    def g(name):
        """ Collision-free version of f.
        """
        if name is None:
            return None
        if name in renamed:
            return renamed[name]
        new_name = _make_unique_name(seen, f(name))
        renamed[name] = new_name
        return new_name

    for op in ops:
        inputs = list(op.input)
        outputs = list(op.output)
        # X: remove all the inputs and outputs
        del op.input[:]
        del op.output[:]
        op.input.extend(g(name) for name in inputs)
        op.output.extend(g(name) for name in outputs)

    if track_blob_names:
        _remap_keys(track_blob_names, g)
    # Rename all operator names (if any) independently so that the
    # unique-fixation happens only once in _fill_missing_operator_names().
    seen.clear()
    renamed.clear()
    for op in ops:
        op.name = g(op.name)

def _replace_colons(track_blob_names, ops):
    """
    `:i` has a special meaning in Tensorflow.
    """
    def f(name):
        return name.replace(':', '$')
    _rename_all(track_blob_names, ops, f)

def _formalize_for_tensorflow(track_blob_names, ops):
    # X: formalize weights and biases
    WEIGHT = re.compile(r"(_w)$")
    WEIGHT_ = re.compile(r"(_w_)")
    BN = re.compile(r"(_bn)$")
    BN_ = re.compile(r"(_bn_)")
    BIAS = re.compile(r"(_b)$")
    BIAS_ = re.compile(r"(_b_)")
    SCALE = re.compile(r"(_s)$")
    SCALE_ = re.compile(r"(_s_)")
    SUM = re.compile(r"(_sum)$")
    SUM_ = re.compile(r"(_sum_)")
    BRANCH = re.compile(r"(_branch)")
    def f(name):
        inter_name = WEIGHT_.sub('/weight_', WEIGHT.sub('/weight', name))
        inter_name = BN_.sub('/batchnorm_', 
                                BN.sub('/batchnorm', inter_name))
        inter_name = BIAS_.sub('/bias_', BIAS.sub('/bias', inter_name))
        inter_name = SCALE_.sub('/scale_', SCALE.sub('/scale', inter_name))
        inter_name = SUM_.sub('/sum_', SUM.sub('/sum', inter_name))
        new_name = BRANCH.sub('/branch', inter_name)
        return new_name
    _rename_all(track_blob_names, ops, f)

def _convert_to_ssa(track_blob_names, ops):
    """
    Convert an operator graph to SSA (i.e. out-of-place).
    I.e. blobs will be renamed so that each blob is produced only once.
    """
    ir = core.IR(ops)
    seen = set()
    versioned = {}
    new_track_blob_names = {}

    def ssa_name(name, versions):
        assert name in versions
        version = versions[name]
        if (name, version) in versioned:
            return versioned[(name, version)]
        # X: seems like the ambiguity is already handled
        # Always setting new_name = `{name}_{version}` would work, but we also try
        # to avoid a trailing `_0`, so we have to be careful not to introduce
        # name collisions, such as (foo_1, 0) = foo_1 = (foo, 1).
        # Note: operator names (if any) will be handled later.
        new_name = _make_unique_name(seen, name, min_version=version)
        versioned[(name, version)] = new_name
        if track_blob_names and name in track_blob_names:
            new_track_blob_names[new_name] = track_blob_names[name]
        return new_name

    for (op, ssa) in zip(ops, ir.ssa):
        # X: somehow the magic is already done there
        assert op is ssa.op
        inputs = list(op.input)
        outputs = list(op.output)
        del op.input[:]
        del op.output[:]
        op.input.extend(ssa_name(name, ssa.in_versions) for name in inputs)
        op.output.extend(ssa_name(name, ssa.out_versions) for name in outputs)

    if track_blob_names:
        track_blob_names.clear()
        track_blob_names.update(new_track_blob_names)

def _add_gradient_scope(track_blob_names, ops):
    """Separate out gradient and momentum for names."""
    def f(name):
        new_name = name
        if '_grad' in name:
            new_name = 'Gradients/{}'.format(new_name.replace('_grad',''))
        if '_momentum' in name:
            new_name = 'Momentum/{}'.format(new_name.replace('_momentum',''))
        return new_name
    _rename_all(track_blob_names, ops, f)

def _tf_device(device_option):
    if not device_option.HasField("device_type"):
        return ""
    if device_option.device_type == caffe2_pb2.CPU:
        return "/cpu:*"
    if device_option.device_type == caffe2_pb2.CUDA:
        return "/gpu:{}".format(device_option.cuda_gpu_id)
    raise Exception("Un-handled device", device_option)

def _add_tf_shape(m, ints):
    sh = TensorShapeProto()
    for i in ints:
        dim = TensorShapeProto.Dim()
        dim.size = i
        sh.dim.extend([dim])
    m['_output_shapes'].list.shape.extend([sh])

def _set_tf_attr(m, arg):
    k = arg.name
    if k == 'shape' and arg.ints:
        _add_tf_shape(m, arg.ints)
        return
    # float
    if arg.HasField("f"):
        m[k].f = arg.f
        return
    # integer
    if arg.HasField("i"):
        m[k].i = arg.i
        return
    # string
    if arg.HasField("s"):
        m[k].s = (
            arg.s if isinstance(arg.s, bytes) else str(arg.s).encode('utf-8'))
        return
    if arg.floats:
        m[k].list.f.extend(arg.floats)
        return
    if arg.ints:
        m[k].list.i.extend(arg.ints)
        return
    if arg.strings:
        m[k].list.s.extend(
            s if isinstance(s, bytes) else str(s).encode('utf-8')
            for s in arg.strings)
        return
    # The value is an empty list.
    m[k].list.s.extend([])

def _operator_to_node(op, inter_blobs, seen):
    # X: no need to assert op name
    assert op
    nodes = []
    outputs = [o for o in op.output if o not in inter_blobs]
    seen.update(outputs)
    len_outputs = len(outputs)
    if len_outputs == 1:
        n = NodeDef()
        n.name = outputs[0]
        # X: we are sure the name is unique
        n.input.extend(op.input)
        # X: does include op as the type
        n.op = op.type
        n.device = _tf_device(op.device_option)
        for arg in op.arg:
            _set_tf_attr(n.attr, arg)
        nodes.append(n)
    elif len_outputs > 1:
        # X: create a name that is likely unique
        if op.name:
            name = op.name
        else:
            l = [name for name in outputs]
            scope = os.path.commonprefix(l)
            name = os.path.join(scope, op.type)
        assert(name)
        op.name = _make_unique_name(seen, name)
        device = _tf_device(op.device_option)
        # X: create additional output nodes
        for output in outputs:
            n = NodeDef()
            n.name = output
            n.input.extend([op.name])
            n.op = 'Blob'
            n.device = device
            nodes.append(n)

        # X: nodes for the current op
        n = NodeDef()
        n.name = op.name
        n.input.extend(op.input)
        # X: does include op as the type
        n.op = op.type
        n.device = device
        for arg in op.arg:
            _set_tf_attr(n.attr, arg)
        nodes.append(n)
    # for other cases it does not matter
    return nodes

def _input_blob_to_node(name):
    assert name
    n = NodeDef()
    n.name = name
    n.op = 'Placeholder'
    return n

# X: remove the debug information, they are copious
def _clear_debug_info(ops):
    for op in ops:
        if op.HasField('debug_info'):
            op.ClearField('debug_info')

def _get_gpu_zero(track_blob_names, ops):
    # X: check if it starts with gpu zero
    def f(op):
        output = str(op.output[0])
        if output.startswith('gpu_0/'):
            return True
        return False
    new_ops = [op for op in ops if f(op)]
    # since the scope is fixed, it is useless now
    GPU0 = re.compile(r"^(gpu_0/)")
    def g(name):
        new_name = GPU0.sub('', name)
        return new_name
    _rename_all(track_blob_names, new_ops, g)
    return new_ops

def _remove_unwanted(ops):
    # remove unwanted things
    def f(blob):
        flag = True
        flag &= blob.find('__m') < 0
        flag &= not blob.startswith('_gpu')
        return flag

    new_ops = []
    for op in ops:
        inputs = list(op.input)
        outputs = list(op.output)
        # X: remove all the inputs and outputs
        del op.input[:]
        del op.output[:]
        new_inputs = [i for i in inputs if f(i)]
        new_outputs = [o for o in outputs if f(o)]
        # X: only add the op if output is not empty
        if new_outputs:
            op.input.extend(new_inputs)
            op.output.extend(new_outputs)
            new_ops.append(op)

    return new_ops

# X: compute the input and output blobs
def _compute_in_out(ops):
    in_blobs = set()
    out_blobs = set()

    for op in ops:
        for input_blob in op.input:
            in_blobs.add(input_blob)
        for output_blob in op.output:
            out_blobs.add(output_blob)

    input_blobs = list(in_blobs.difference(out_blobs))
    output_blobs = list(out_blobs.difference(in_blobs))
    inter_blobs = { b:1 for b in output_blobs if b.startswith('_') }
    # X: now reset the actual output
    output_blobs = [ b for b in output_blobs if b not in inter_blobs ]

    return input_blobs, inter_blobs, output_blobs

# ops are necessary
def _operators_to_graph_def(ops,
                            clear_debug_info=True,
                            single_gpu=False,
                            remove_unwanted=True,
                            with_gradient_scope=True,
                            custom_rename=None):
    # X: this is to track how the blob names are changed
    # X: each key is the final name, and each value is the original name
    track_blob_names = {}
    track_blob_names.update(_get_blob_names(ops))
    if clear_debug_info:
        _clear_debug_info(ops)
    # X: if the architecture is crated by some 
    if single_gpu:
        ops = _get_gpu_zero(track_blob_names, ops)
    if remove_unwanted:
        # X: for now we will still keep the inputs from other gpus
        ops = _remove_unwanted(ops)
    # X: this is necessary since caffe can have in-place operator
    _convert_to_ssa(track_blob_names, ops)
    # X: first replace weights and biases, so they look similar
    _formalize_for_tensorflow(track_blob_names, ops)
    # X: this is to necessary
    _replace_colons(track_blob_names, ops)
    # X: allow an extra function to rename and customize
    # X: it comes first before the entire scope is changed by gradient
    if custom_rename:
        _rename_all(track_blob_names, ops, custom_rename)
    # X: special handles for gradients related
    if with_gradient_scope:
        _add_gradient_scope(track_blob_names, ops)

    input_blobs, inter_blobs, _ = _compute_in_out(ops)
    # X: apparently the external inputs are missing
    current_graph = GraphDef(versions=VersionDef(producer=22))
    # X: update nodes with the external inputs
    seen = set(input_blobs)
    for blob in input_blobs:
        current_graph.node.extend([_input_blob_to_node(blob)])
    # X: update nodes with other nodes
    for op in ops:
        current_graph.node.extend(_operator_to_node(op, inter_blobs, seen))

    return current_graph, track_blob_names

def model_to_graph(model, **kwargs):
    # X: for some reason it needs to get the initialization operations as well 
    nets = [model.param_init_net, model.net]
    return nets_to_graph(nets, **kwargs)

def nets_to_graph(nets, **kwargs):
    # X: get the network proto
    nets = [copy.deepcopy(net.Proto()) for net in nets]
    return protos_to_graph(nets, **kwargs)

def protos_to_graph(nets, **kwargs):
    for net in nets:
        _propagate_device_option(net)
    ops = [op for net in nets for op in net.op]
    # X: ignore the output there, should be inferred
    return _operators_to_graph_def(ops, **kwargs)