"""Models that support pickling

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import numpy as np

from cleverhans.model import Model
from cleverhans.serial import PicklableVariable as PV
from cleverhans.utils import ordered_union


class PicklableModel(Model):
    """
    A Model that supports pickling.

    Subclasses of this model must use only PicklableVariable and must refer
    to their variables only by referencing the Python objects, not using
    TensorFlow names (so no variable scopes). Pickle cannot find variables
    referenced only by name and thus may fail to save them. Pickle may not
    be able to get the original name back when restoring the variable so the
    names should not be relied on.
    """

    def __init__(self):
        super(PicklableModel, self).__init__()
        del self.scope  # Must not use Variable scopes / names for anything

    def get_params(self):
        raise NotImplementedError(str(type(self)) + " does not implement"
                                  " get_params.")


class MLP(PicklableModel):
    """
    A picklable multilayer perceptron
    """

    def __hash__(self):
        return hash(id(self))

    def __init__(self, layers, input_shape):
        super(MLP, self).__init__()

        self.layer_names = []
        self.layers = layers
        self.input_shape = input_shape

        if isinstance(layers[-1], Softmax):
            if not hasattr(layers[-1], 'name'):
                layers[-1].name = 'probs'
            if not hasattr(layers[-2], 'name'):
                layers[-2].name = 'logits'
        else:
            if not hasattr(layers[-1], 'name'):
                layers[-1].name = 'logits'
        for i, layer in enumerate(self.layers):
            if layer.parent is None:
                if i == 0:
                    layer.parent = "input"
                else:
                    layer.parent = layers[i - 1].name
            if hasattr(layer, 'name'):
                name = layer.name
            else:
                name = layer.__class__.__name__ + str(i)
                layer.name = name
            self.layer_names.append(name)

            layer.set_input_shape(input_shape)
            input_shape = layer.get_output_shape()

    def get_params(self):
        out = []
        for layer in self.layers:
            out = ordered_union(out, layer.get_params())
        return out

    def fprop(self, x=None, given=None):

        # Note: this currently isn't great.
        # A layer can have any parent it wants, but the parent
        # must come earlier in the list.
        # There's no way to have > 1 parent.
        # This means we can support branched structures that split,
        # e.g. for multiple output heads, but not structures
        # that converge.
        # We can feed a value in the middle using "given" but
        # only layers after the given one are run using the current
        # implementation, so the feed must happen before any branch
        # point.

        if x is None:
            if given is None:
                raise ValueError("One of `x` or `given` must be specified")
        else:
            assert given is None
            given = ('input', x)
        name, value = given
        out = {name: value}
        x = value

        if name == 'input':
            layers = self.layers
        else:
            for i, layer in enumerate(self.layers[:-1]):
                if layer.name == name:
                    layers = self.layers[i+1:]
                    break

        for layer in layers:
            x = out[layer.parent]
            x = layer.fprop(x)
            assert x is not None
            out[layer.name] = x
        return out


class Layer(PicklableModel):
    def __init__(self, name=None, parent=None):
        if name is not None:
            self.name = name
        self.parent = parent

    def get_output_shape(self):
        return self.output_shape


class Linear(Layer):

    def __init__(self, num_hid, init_scale=1., init_b=0., **kwargs):
        super(Linear, self).__init__(**kwargs)
        self.num_hid = num_hid
        self.init_scale = init_scale
        self.init_b = init_b

    def set_input_shape(self, input_shape):
        batch_size, dim = input_shape
        self.input_shape = [batch_size, dim]
        self.output_shape = [batch_size, self.num_hid]
        init = tf.random_normal([dim, self.num_hid], dtype=tf.float32)
        init = init / tf.sqrt(1e-7 + tf.reduce_sum(tf.square(init), axis=0,
                                                   keep_dims=True))
        init = init * self.init_scale
        self.W = PV(init)
        self.b = PV((np.zeros((self.num_hid,))
                     + self.init_b).astype('float32'))

    def fprop(self, x):
        return tf.matmul(x, self.W.var) + self.b.var

    def get_params(self):
        return [self.W.var, self.b.var]


class Conv2D(Layer):

    def __init__(self, output_channels, kernel_shape, strides, padding,
                 use_bias=False, init_scale=1., **kwargs):
        self.__dict__.update(locals())
        del self.self
        super(Conv2D, self).__init__(**kwargs)

    def set_input_shape(self, input_shape):
        batch_size, rows, cols, input_channels = input_shape
        kernel_shape = tuple(self.kernel_shape) + (input_channels,
                                                   self.output_channels)
        assert len(kernel_shape) == 4
        assert all(isinstance(e, int) for e in kernel_shape), kernel_shape
        init = tf.random_normal(kernel_shape, dtype=tf.float32)
        init = self.init_scale * init / tf.sqrt(1e-7 +
                                                tf.reduce_sum(tf.square(init),
                                                              axis=(0, 1, 2)))
        self.kernels = PV(init)
        if self.use_bias:
            self.b = PV(np.zeros((self.output_channels,)).astype('float32'))
        input_shape = list(input_shape)
        orig_batch_size = input_shape[0]
        input_shape[0] = 1
        dummy_batch = tf.zeros(input_shape)
        dummy_output = self.fprop(dummy_batch)
        output_shape = [int(e) for e in dummy_output.get_shape()]
        output_shape[0] = orig_batch_size
        self.output_shape = tuple(output_shape)

    def fprop(self, x):
        out = tf.nn.conv2d(x, self.kernels.var,
                           (1,) + tuple(self.strides) + (1,), self.padding)
        if self.use_bias:
            out = out + self.b.var
        return out

    def get_params(self):
        out = [self.kernels.var]
        if self.use_bias:
            out.append(self.b.var)
        return out


class ReLU(Layer):

    def __init__(self, leak=0., **kwargs):
        super(ReLU, self).__init__(**kwargs)
        self.leak = leak

    def set_input_shape(self, shape):
        self.input_shape = shape
        self.output_shape = shape

    def get_output_shape(self):
        return self.output_shape

    def fprop(self, x):
        out = tf.nn.relu(x)
        if self.leak != 0.0:
            out = out - self.leak * tf.nn.relu(-x)
        return out

    def get_params(self):
        return []


class Sigmoid(Layer):

    def __init__(self, **kwargs):
        super(Sigmoid, self).__init__(**kwargs)

    def set_input_shape(self, shape):
        self.input_shape = shape
        self.output_shape = shape

    def get_output_shape(self):
        return self.output_shape

    def fprop(self, x):
        return tf.nn.sigmoid(x)

    def get_params(self):
        return []


class Tanh(Layer):

    def __init__(self, **kwargs):
        super(Tanh, self).__init__(**kwargs)

    def set_input_shape(self, shape):
        self.input_shape = shape
        self.output_shape = shape

    def get_output_shape(self):
        return self.output_shape

    def fprop(self, x):
        return tf.nn.tanh(x)

    def get_params(self):
        return []


class LeakyReLU(ReLU):

    def __init__(self, leak=.2, **kwargs):
        super(LeakyReLU, self).__init__(leak=leak, **kwargs)


class ELU(Layer):

    def set_input_shape(self, shape):
        self.input_shape = shape
        self.output_shape = shape

    def get_output_shape(self):
        return self.output_shape

    def fprop(self, x):
        return tf.nn.elu(x)

    def get_params(self):
        return []


class SELU(Layer):

    def __init__(self):
        pass

    def set_input_shape(self, shape):
        self.input_shape = shape
        self.output_shape = shape

    def get_output_shape(self):
        return self.output_shape

    def fprop(self, x):
        alpha = 1.6732632423543772848170429916717
        scale = 1.0507009873554804934193349852946
        mask = tf.to_float(x >= 0.)
        out = mask * x + (1. - mask) * \
            (alpha * tf.exp((1. - mask) * x) - alpha)
        return scale * out

    def get_params(self):
        return []


class TanH(Layer):

    def __init__(self):
        pass

    def set_input_shape(self, shape):
        self.input_shape = shape
        self.output_shape = shape

    def get_output_shape(self):
        return self.output_shape

    def fprop(self, x):
        return tf.nn.tanh(x)


class Softmax(Layer):

    def set_input_shape(self, shape):
        self.input_shape = shape
        self.output_shape = shape

    def fprop(self, x):
        out = tf.nn.softmax(x)
        return out

    def get_params(self):
        return []


class Flatten(Layer):

    def set_input_shape(self, shape):
        self.input_shape = shape
        output_width = 1
        for factor in shape[1:]:
            output_width *= factor
        self.output_width = output_width
        self.output_shape = [None, output_width]

    def fprop(self, x):
        return tf.reshape(x, [-1, self.output_width])

    def get_params(self):
        return []


class Add(Layer):
    """
    A Layer that adds a function to its input.
    The function to add is specified in terms of multiple layers, just like
    in the MLP class.
    The Add layer is useful for implementing residual networks.
    """

    def __hash__(self):
        return hash(id(self))

    def set_input_shape(self, shape):
        self.input_shape = shape
        shapes = {"input": shape}
        for layer in self.layers:
            layer.set_input_shape(shapes[layer.parent])
            shapes[layer.name] = layer.get_output_shape()
        self.output_shape = shapes[self.layers[-1].name]

    def __init__(self, layers):
        super(Add, self).__init__()

        self.layer_names = []
        self.layers = layers

        for i, layer in enumerate(self.layers):
            if layer.parent is None:
                if i == 0:
                    layer.parent = "input"
                else:
                    layer.parent = layers[i - 1].name
            if hasattr(layer, 'name'):
                name = layer.name
            else:
                name = layer.__class__.__name__ + str(i)
                layer.name = name
            self.layer_names.append(name)

    def get_params(self):
        out = []
        for layer in self.layers:
            out = ordered_union(out, layer.get_params())
        return out

    def fprop(self, x):

        orig_x = x

        # Note: this currently isn't great.
        # A layer can have any parent it wants, but the parent
        # must come earlier in the list.
        # There's no way to have > 1 parent.
        # This means we can support branched structures that split,
        # e.g. for multiple output heads, but not structures
        # that converge.
        # We can feed a value in the middle using "given" but
        # only layers after the given one are run using the current
        # implementation, so the feed must happen before any branch
        # point.

        out = {'input': x}

        for layer in self.layers:
            x = out[layer.parent]
            x = layer.fprop(x)
            assert x is not None
            out[layer.name] = x

        return orig_x + out[self.layers[-1].name]
