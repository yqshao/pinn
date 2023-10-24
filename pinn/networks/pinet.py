# -*- coding: utf-8 -*-

import tensorflow as tf
from pinn.utils import pi_named, connect_dist_grad
from pinn.layers import (
    CellListNL,
    CutoffFunc,
    PolynomialBasis,
    GaussianBasis,
    AtomicOnehot,
    ANNOutput,
)


class FFLayer(tf.keras.layers.Layer):
    R"""`FFLayer` is a shortcut to create a multi-layer perceptron (MLP) or a
    feed-forward network. A `FFLayer` takes one tensor as input of arbitratry
    shape, and parse it to a list of `tf.keras.layers.Dense` layers, specified
    by `n_nodes`. Each dense layer transforms the input variable as:

    $$
    \begin{aligned}
    \mathbb{X}'_{\ldots{}\beta} &= \mathrm{Dense}(\mathbb{X}_{\ldots{}\alpha}) \\
      &= h\left( \sum_\alpha W_{\alpha\beta} \mathbb{X}_{\ldots{}\alpha} + b_{\beta} \right)
    \end{aligned}
    $$

    , where $W_{\alpha\beta}$, $b_{\beta}$ are the learnable weights and biases,
    $h$ is the activation function, and $\mathbb{X}$ can be
    $\mathbb{P}_{i\alpha}$ or $\mathbb{I}_{ij\alpha}$ with $\alpha,\beta$ being
    the indices of input/output channels. The keyward arguments are parsed into
    the class, which can be used to specify the bias, activation function, etc
    for the dense layer. `FFLayer` outputs a tensor with the shape `[...,
    n_nodes[-1]]`.


    In the PiNet architecture, `PPLayer` and `IILayer` are both instances of the
    `FFLayer` class , namely:

    $$
    \begin{aligned}
      \mathbb{I}_{ij\gamma} &= \mathrm{IILayer}(\mathbb{I}'_{ij\beta}) = \mathrm{FFLayer}(\mathbb{I}'_{ij\beta}) \\
      \mathbb{P}_{i\delta} &= \mathrm{PPLayer}(\mathbb{P}''_{i\gamma}) = \mathrm{FFLayer}(\mathbb{P}'_{i\gamma})
    \end{aligned}
    $$

    , with the difference that `IILayer`s have their baises set to zero to avoid
    discontinuity in the model output.

    """

    def __init__(self, n_nodes=[64, 64], **kwargs):
        """
        Args:
            n_nodes (list): dimension of the layers
            **kwargs (dict): options to be parsed to dense layers
        """
        super(FFLayer, self).__init__()
        self.dense_layers = [
            tf.keras.layers.Dense(n_node, **kwargs) for n_node in n_nodes
        ]

    def call(self, tensor):
        """
        Args:
            tensor (tensor): input tensor

        Returns:
            tensor (tensor): tensor with shape `(...,n_nodes[-1])`
        """
        for layer in self.dense_layers:
            tensor = layer(tensor)
        return tensor


class PILayer(tf.keras.layers.Layer):
    R"""`PILayer` takes the properties ($\mathbb{P}_{i\alpha},
    \mathbb{P}_{j\alpha}$) of a pair of atoms as input and outputs a set of
    interactions for each pair. The inputs will be broadcasted and concatenated
    as the input of a feed-forward neural network (`FFLayer`), and the
    interactions are generated by taking the output of the `FFLayer` as weights
    of radial basis functions, i.e.:

    $$
    \begin{aligned}
    w_{ij(b\beta)} &= \mathrm{FFLayer}\left((\mathbf{1}_{j}\mathbb{P}_{i\alpha})\Vert(\mathbf{1}_{i}\mathbb{P}_{j\alpha})\right) \\
    \mathbb{I}'_{ij\beta} &= \sum_b W_{ij(b\beta)} \, e_{ijb}
    \end{aligned}
    $$

    , where $w_{ij(b\beta)}$ is an intemediate weight tensor for the
    radial basis functions, output by the `FFLayer`; the output channel is
    reshaped into two dimensions, where $b$ is the index for the basis function
    and $d$ is the index for output interaction.


    `n_nodes` specifies the number of nodes in the `FFLayer`. Note that the last
    element of n_nodes specifies the number of output channels after applying
    the basis function ($d$ instead of $bd$), i.e. the output dimension of
    FFLayer is `[n_pairs,n_nodes[-1]*n_basis]`, the output is then summed with
    the basis to form the output interaction.

    """

    def __init__(self, n_nodes=[64], **kwargs):
        """
        Args:
            n_nodes (list of int): number of nodes to use
            **kwargs (dict): keyword arguments will be parsed to the feed forward layers
        """
        super(PILayer, self).__init__()
        self.n_nodes = n_nodes
        self.kwargs = kwargs

    def build(self, shapes):
        """"""
        self.n_basis = shapes[2][-1]
        n_nodes_iter = self.n_nodes.copy()
        n_nodes_iter[-1] *= self.n_basis
        self.ff_layer = FFLayer(n_nodes_iter, **self.kwargs)

    def call(self, tensors):
        """
        PILayer take a list of three tensors as input:

        - ind_2: [sparse indices](layers.md#sparse-indices) of pairs with shape `(n_pairs, 2)`
        - prop: property tensor with shape `(n_atoms, n_prop)`
        - basis: interaction tensor with shape `(n_pairs, n_basis)`

        Args:
            tensors (list of tensors): list of `[ind_2, prop, basis]` tensors

        Returns:
            inter (tensor): interaction tensor with shape `(n_pairs, n_nodes[-1])`
        """
        ind_2, prop, basis = tensors
        ind_i = ind_2[:, 0]
        ind_j = ind_2[:, 1]
        prop_i = tf.gather(prop, ind_i)
        prop_j = tf.gather(prop, ind_j)

        inter = tf.concat([prop_i, prop_j], axis=-1)
        inter = self.ff_layer(inter)
        inter = tf.reshape(inter, [-1, self.n_nodes[-1], self.n_basis])
        inter = tf.einsum("pcb,pb->pc", inter, basis)
        return inter


class IPLayer(tf.keras.layers.Layer):
    R"""The IPLayer transforms pairwise interactions to atomic properties

    The IPLayer has no learnable variables and simply sums up the pairwise
    interations. Thus the returned property has the same shape with the
    input interaction, i.e.:

    $$
    \begin{aligned}
    \mathbb{P}_{i\gamma} = \mathrm{IPLayer}(\mathbb{I}_{ij\gamma}) = \sum_{j} \mathbb{I}_{ij\gamma}
    \end{aligned}
    $$

    """

    def __init__(self):
        """
        IPLayer does not require any parameter, initialize as `IPLayer()`.
        """
        super(IPLayer, self).__init__()

    def call(self, tensors):
        """
        IPLayer take a list of three tensors list as input:

        - ind_2: [sparse indices](layers.md#sparse-indices) of pairs with shape `(n_pairs, 2)`
        - prop: property tensor with shape `(n_atoms, n_prop)`
        - inter: interaction tensor with shape `(n_pairs, n_inter)`

        Args:
            tensors (list of tensor): list of [ind_2, prop, inter] tensors

        Returns:
            prop (tensor): new property tensor with shape `(n_atoms, n_inter)`
        """
        ind_2, prop, inter = tensors
        n_atoms = tf.shape(prop)[0]
        return tf.math.unsorted_segment_sum(inter, ind_2[:, 0], n_atoms)


class OutLayer(tf.keras.layers.Layer):
    """`OutLayer` updates the network output with a `FFLayer` layer, where the
    `out_units` controls the dimension of outputs. In addition to the `FFLayer`
    specified by `n_nodes`, the `OutLayer` has one additional linear biasless
    layer that scales the outputs, specified by `out_units`.

    """

    def __init__(self, n_nodes, out_units, **kwargs):
        """
        Args:
            n_nodes (list): dimension of the hidden layers
            out_units (int): dimension of the output units
            **kwargs (dict): options to be parsed to dense layers
        """
        super(OutLayer, self).__init__()
        self.out_units = out_units
        self.ff_layer = FFLayer(n_nodes, **kwargs)
        self.out_units = tf.keras.layers.Dense(
            out_units, activation=None, use_bias=False
        )

    def call(self, tensors):
        """
        OutLayer takes a list of three tensors as input:

        - ind_1: [sparse indices](layers.md#sparse-indices) of atoms with shape `(n_atoms, 2)`
        - prop: property tensor with shape `(n_atoms, n_prop)`
        - prev_output:  previous output with shape `(n_atoms, out_units)`

        Args:
            tensors (list of tensors): list of [ind_1, prop, prev_output] tensors

        Returns:
            output (tensor): an updated output tensor with shape `(n_atoms, out_units)`
        """
        ind_1, prop, prev_output = tensors
        prop = self.ff_layer(prop)
        output = self.out_units(prop) + prev_output
        return output


class GCBlock(tf.keras.layers.Layer):
    def __init__(self, pp_nodes, pi_nodes, ii_nodes, **kwargs):
        super(GCBlock, self).__init__()
        iiargs = kwargs.copy()
        iiargs.update(use_bias=False)
        self.pp_layer = FFLayer(pp_nodes, **kwargs)
        self.pi_layer = PILayer(pi_nodes, **kwargs)
        self.ii_layer = FFLayer(ii_nodes, **iiargs)
        self.ip_layer = IPLayer()

    def call(self, tensors):
        ind_2, prop, basis = tensors
        prop = self.pp_layer(prop)
        inter = self.pi_layer([ind_2, prop, basis])
        inter = self.ii_layer(inter)
        prop = self.ip_layer([ind_2, prop, inter])
        return prop


class ResUpdate(tf.keras.layers.Layer):
    R"""`ResUpdate` layer implements ResNet-like update of properties that
    addresses vanishing/exploding gradient problems (see
    [arXiv:1512.03385](https://arxiv.org/abs/1512.03385)).

    It takes two tensors (old and new) as input, the tensors should have the
    same shape except for the last dimension, and a tensor with the shape of the
    new tensor is always returned.

    If shapes of the two tensors match, their sum is returned. If the two
    tensors' shapes differ in the last dimension, the old tensor will be added
    to the new after a learnable linear transformation that matches its shape to
    the new tensor, i.e., according to the above flowchart:

    $$
    \begin{aligned}
    \mathbb{P}'_{i\gamma} &= \mathrm{ResUpdate}(\mathbb{P}^{t}_{i\alpha},\mathbb{P}''_{i\gamma}) & \\
      &= \begin{cases}
           \mathbb{P}^{t}_{i\alpha} + \mathbb{P}''_{i\gamma} & \textrm{, if } \mathrm{dim}(\mathbb{P}^{t}) = \mathrm{dim}(\mathbb{P}'')\\
           \sum_{\alpha} W_{\alpha\gamma} \, \mathbb{P}^{t}_{i\alpha} + \mathbb{P}''_{i\gamma} & \textrm{, if } \mathrm{dim}(\mathbb{P}^{t}) \ne \mathrm{dim}(\mathbb{P}'')
         \end{cases}
    \end{aligned}
    $$

    , where $W_{\alpha\beta}$ is a learnable weight matrix if needed.

    In the PiNet architecture above, ResUpdate is only used to update the
    properties after the `IPLayer`, when `ii_nodes[-1]==pp_nodes[-1]`, the
    weight matrix is only necessary at $t=0$.
    """

    def __init__(self):
        """
        ResUpdate does not require any parameter, initialize as `ResUpdate()`.
        """
        super(ResUpdate, self).__init__()

    def build(self, shapes):
        """"""
        assert isinstance(shapes, list) and len(shapes) == 2
        if shapes[0][-1] == shapes[1][-1]:
            self.transform = lambda x: x
        else:
            self.transform = tf.keras.layers.Dense(
                shapes[1][-1], use_bias=False, activation=None
            )

    def call(self, tensors):
        """
        Args:
           tensors (list of tensors): two tensors with matching shapes expect the last dimension

        Returns:
           tensor (tensor): updated tensor with the same shape as the second input tensor
        """
        old, new = tensors
        return self.transform(old) + new


class PreprocessLayer(tf.keras.layers.Layer):
    def __init__(self, atom_types, rc):
        super(PreprocessLayer, self).__init__()
        self.embed = AtomicOnehot(atom_types)
        self.nl_layer = CellListNL(rc)

    def call(self, tensors):
        tensors = tensors.copy()
        for k in ["elems", "dist"]:
            if k in tensors.keys():
                tensors[k] = tf.reshape(tensors[k], tf.shape(tensors[k])[:1])
        if "ind_2" not in tensors:
            tensors.update(self.nl_layer(tensors))
            tensors["prop"] = tf.cast(
                self.embed(tensors["elems"]), tensors["coord"].dtype
            )
        return tensors


class PiNet(tf.keras.Model):
    """This class implements the Keras Model for the PiNet network."""

    def __init__(
        self,
        atom_types=[1, 6, 7, 8],
        rc=4.0,
        cutoff_type="f1",
        basis_type="polynomial",
        n_basis=4,
        gamma=3.0,
        center=None,
        pp_nodes=[16, 16],
        pi_nodes=[16, 16],
        ii_nodes=[16, 16],
        out_nodes=[16, 16],
        out_units=1,
        out_pool=False,
        act="tanh",
        depth=4,
    ):
        """
        Args:
            atom_types (list): elements for the one-hot embedding
            pp_nodes (list): number of nodes for PPLayer
            pi_nodes (list): number of nodes for PILayer
            ii_nodes (list): number of nodes for IILayer
            out_nodes (list): number of nodes for OutLayer
            out_pool (str): pool atomic outputs, see ANNOutput
            depth (int): number of interaction blocks
            rc (float): cutoff radius
            basis_type (string): basis function, can be "polynomial" or "gaussian"
            n_basis (int): number of basis functions to use
            gamma (float|array): width of gaussian function for gaussian basis
            center (float|array): center of gaussian function for gaussian basis
            cutoff_type (string): cutoff function to use with the basis.
            act (string): activation function to use
        """
        super(PiNet, self).__init__()

        self.depth = depth
        self.preprocess = PreprocessLayer(atom_types, rc)
        self.cutoff = CutoffFunc(rc, cutoff_type)

        if basis_type == "polynomial":
            self.basis_fn = PolynomialBasis(n_basis)
        elif basis_type == "gaussian":
            self.basis_fn = GaussianBasis(center, gamma, rc, n_basis)

        self.res_update = [ResUpdate() for i in range(depth)]
        self.gc_blocks = [GCBlock([], pi_nodes, ii_nodes, activation=act)]
        self.gc_blocks += [
            GCBlock(pp_nodes, pi_nodes, ii_nodes, activation=act)
            for i in range(depth - 1)
        ]
        self.out_layers = [OutLayer(out_nodes, out_units) for i in range(depth)]
        self.ann_output = ANNOutput(out_pool)

    def call(self, tensors):
        """PiNet takes batches atomic data as input, the following keys are
        required in the input dictionary of tensors:

        - `ind_1`: [sparse indices](layers.md#sparse-indices) for the batched data, with shape `(n_atoms, 1)`;
        - `elems`: element (atomic numbers) for each atom, with shape `(n_atoms)`;
        - `coord`: coordintaes for each atom, with shape `(n_atoms, 3)`.

        Optionally, the input dataset can be processed with
        `PiNet.preprocess(tensors)`, which adds the following tensors to the
        dictionary:

        - `ind_2`: [sparse indices](layers.md#sparse-indices) for neighbour list, with shape `(n_pairs, 2)`;
        - `dist`: distances from the neighbour list, with shape `(n_pairs)`;
        - `diff`: distance vectors from the neighbour list, with shape `(n_pairs, 3)`;
        - `prop`: initial properties `(n_pairs, n_elems)`;

        Args:
            tensors (dict of tensors): input tensors

        Returns:
            output (tensor): output tensor with shape `[n_atoms, out_nodes]`
        """
        tensors = self.preprocess(tensors)
        fc = self.cutoff(tensors["dist"])
        basis = self.basis_fn(tensors["dist"], fc=fc)
        output = 0.0
        for i in range(self.depth):
            prop = self.gc_blocks[i]([tensors["ind_2"], tensors["prop"], basis])
            output = self.out_layers[i]([tensors["ind_1"], prop, output])
            tensors["prop"] = self.res_update[i]([tensors["prop"], prop])

        output = self.ann_output([tensors["ind_1"], output])
        return output
