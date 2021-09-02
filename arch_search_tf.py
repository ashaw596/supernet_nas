from functools import total_ordering
from typing import Dict, Optional, List, Union

import torch
import torch.nn.functional
from tensorflow import keras
import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow.python.framework.convert_to_constants import convert_variables_to_constants_v2_as_graph


@total_ordering
class NasModel(keras.Model):
    def __init__(self, model):
        super().__init__()
        self.mod = model

    def compile(self, *inputs, arch_optimizer, **kwargs):
        self.arch_optimizer = arch_optimizer

        self.arch_params = []
        for mod in self.submodules:
            if isinstance(mod, MixedModuleTf):
                assert mod.built
                self.arch_params.append(mod.gumble_arch_params)
        self.non_arch_params = []
        for v in self.trainable_variables:
            if not any(v is arch_param for arch_param in self.arch_params):
                self.non_arch_params.append(v)

        assert len(self.trainable_variables) == len(self.arch_params) + len(self.non_arch_params)

        self.concat_params = self.non_arch_params + self.arch_params

        print("Arch Parameters:", len(self.arch_params))
        print("Non-arch Parameters:", len(self.non_arch_params))
        print("All Parameters:", len(self.concat_params))

        super().compile(*inputs, **kwargs)

    def train_step(self, data, slow_assert=False):
        # Unpack the data. Its structure depends on your model and
        # on what you pass to `fit()`.
        x, y = data

        with tf.GradientTape() as tape:
            y_pred = self(x, training=True)  # Forward pass
            # Compute the loss value
            # (the loss function is configured in `compile()`)
            loss = self.compiled_loss(y, y_pred, regularization_losses=self.losses)

        gradients = tape.gradient(loss, self.concat_params)

        non_arch_gradients = gradients[0:len(self.non_arch_params)]
        arch_gradients = gradients[len(self.non_arch_params):]

        if slow_assert:
            assert all(first is second for first, second in zip(self.non_arch_params, self.concat_params[0:len(self.non_arch_params)]))
            assert all(first is second for first, second in zip(self.arch_params, self.concat_params[len(self.non_arch_params):]))

        # Compute gradients
        non_arch_params = self.non_arch_params
        # Update non arch weights
        self.optimizer.apply_gradients(zip(non_arch_gradients, non_arch_params))

        arch_params = self.arch_params
        # Update arch weights
        self.arch_optimizer.apply_gradients(zip(arch_gradients, arch_params))


        # Update metrics (includes the metric that tracks the loss)
        self.compiled_metrics.update_state(y, y_pred)
        # Return a dict mapping metric names to current value
        return {m.name: m.result() for m in self.metrics}

    def __gt__(self, other):
        return id(self) > id(other)

    def call(self, inp):
        return self.mod(inp)

def get_flops_inputs(model, input_shape):

    real_model = tf.function(model).get_concrete_function(tf.TensorSpec(input_shape, tf.float32))
    frozen_func, graph_def = convert_variables_to_constants_v2_as_graph(real_model)

    run_meta = tf.compat.v1.RunMetadata()
    opts = tf.compat.v1.profiler.ProfileOptionBuilder.float_operation()
    flops = tf.compat.v1.profiler.profile(graph=frozen_func.graph,
                                            run_meta=run_meta, cmd='op', options=opts)
    return flops.total_float_ops

class MixedModuleTf(keras.layers.Layer):
    def __init__(self, ops: Union[List[keras.layers.Layer], Dict[str, keras.layers.Layer]],
                 cost_loss_multiplier=0.0):
        super().__init__()
        if isinstance(ops, list):
            ops = {str(i): op for i, op in enumerate(ops)}
        assert len(ops) > 1
        for name, module in ops.items():
            self.add_module(name, module)
        self.op_names = list(ops.keys())
        self.cost_loss_multiplier = cost_loss_multiplier

    def build(self, input_shape):
        print("Build ")
        self.gumbel_temperature = self.add_weight(
            shape=(),
            initializer=tf.constant_initializer(1),
            trainable=False
        )

        # TODO(ashaw596): Figure out what to do about op cost
        self.ops_cost_static = self.add_weight(
            shape=(1, len(self.op_names)),
            initializer=tf.constant_initializer(0),
            trainable=False
        )

        self.gumble_arch_params = self.add_weight(
            shape=(len(self.op_names)),
            initializer=tf.constant_initializer(1),
            trainable=True
        )

        self.gumbel_dist = tfp.distributions.RelaxedOneHotCategorical(
            logits=self.gumble_arch_params,
            temperature=self.gumbel_temperature,
        )

        for name in self.op_names:
            self.get_module(name).build(input_shape)
        super().build(input_shape)

        for i, op_name in enumerate(self.op_names):
            flops = get_flops_inputs(self.get_module(op_name), [1] + list(input_shape[1:]))
            self.ops_cost_static[0,i].assign(flops)
            print("flops", op_name, flops)

        print(self.ops_cost_static)
        # self.register_buffer('ops_cost_static', torch.zeros(len(self.ops)))
        # self.gumble_arch_params = torch.nn.Parameter(torch.ones(len(self.ops), 1))
        # self.register_buffer('gumbel_temperature', torch.ones(1))


    def add_module(self, name, module):
        setattr(self, 'sublayer_' + name, module)

    def get_module(self, name):
        return getattr(self, 'sublayer_' + name)


    def call(self, inp, *inputs, **kwargs):
        batch_size = tf.shape(inp)[0]
        gumbel_weights = self.gumbel_dist.sample(batch_size)

        outputs = []
        for i, name in enumerate(self.op_names):
            outputs.append(self.get_module(name)(inp, *inputs, **kwargs))

        concat_outputs = tf.stack(outputs, axis=1)
        print(tf.shape(gumbel_weights))
        orig_shape = tf.shape(gumbel_weights)
        shape = tf.shape(concat_outputs)
        reshaped_gumbel_weights = tf.reshape(gumbel_weights, shape=[orig_shape[0], orig_shape[1]] + [1]*(len(shape) - 2))
        weighted_outputs = reshaped_gumbel_weights * concat_outputs

        output = tf.math.reduce_sum(weighted_outputs, axis=1)

        #TODO(ashaw596): cost loss
        cost = self.ops_cost_static * gumbel_weights
        cost_loss = tf.reduce_mean(tf.reduce_sum(cost * self.cost_loss_multiplier, axis=1))
        self.add_loss(cost_loss)

        return output



class SupernetArchWatcherCallback(keras.callbacks.Callback):
    def __init__(self, model):
        self.gumbel_arch_params = []
        self.op_names = []
        for mod in model.submodules:
            if isinstance(mod, MixedModuleTf):
                assert mod.built
                self.gumbel_arch_params.append(mod.gumble_arch_params)
                self.op_names.append(mod.op_names)

    def on_epoch_end(self, epoch, logs=None):
        genotype = []
        for names, params in zip(self.op_names, self.gumbel_arch_params):
            probs = tf.nn.softmax(params)
            gene = {}
            for i, name in enumerate(names):
                gene[name] = probs[i]
            genotype.append(gene)
        print("Genotype: epoch: ", epoch, genotype)


class SupernetTemperatureCallback(keras.callbacks.Callback):
    def __init__(self, model, start_epoch, final_epoch, start_temp, end_temp):
        self.temperature_variables = []
        for mod in model.submodules:
            if isinstance(mod, MixedModuleTf):
                assert mod.built
                self.temperature_variables.append(mod.gumbel_temperature)

        print("Temperature Variables Found:", len(self.temperature_variables))

        self.start_epoch = start_epoch
        self.final_epoch = final_epoch
        self.start_temp = start_temp
        self.end_temp = end_temp

        assert self.start_temp > self.end_temp

    def on_epoch_begin(self, epoch, logs=None):
        print("Epoch", epoch)

        delta_temp = (self.end_temp - self.start_temp) / (self.final_epoch - self.start_epoch)
        temperature = max(self.start_temp + delta_temp * max(epoch - self.start_epoch, 0), self.end_temp)
        print("Temperature", temperature)

        for temp_var in self.temperature_variables:
            temp_var.assign(temperature)
