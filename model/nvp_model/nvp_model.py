import chainer
import chainer.links as L
import chainer.functions as F

from model.atom_embed.atom_embed import atom_embed_model
from model.hyperparameter import Hyperparameter
from model.nvp_model.coupling import AffineAdjCoupling, AdditiveAdjCoupling, \
    AffineNodeFeatureCoupling, AdditiveNodeFeatureCoupling

import math
import os
import logging as log

class AttentionNvpModel(chainer.Chain):
    def __init__(self, hyperparams):
        super(AttentionNvpModel, self).__init__()
        self.hyperparams = hyperparams
        self.masks = dict()
        self.masks["relation"] = self._create_masks("relation")
        self.masks["feature"] = self._create_masks("feature")
        self.adj_size = self.hyperparams.num_nodes * \
            self.hyperparams.num_nodes * self.hyperparams.num_edge_types
        self.x_size = self.hyperparams.num_nodes * self.hyperparams.num_features

        assert hasattr(self.hyperparams, "embed_model_path") and hasattr(
            self.hyperparams, "embed_model_hyper")
        with self.init_scope():
            self.embed_model = atom_embed_model(
                Hyperparameter(hyperparams.embed_model_hyper))
            assert self.embed_model.word_size == self.hyperparams.num_features
            initial_ln_z_var = math.log(self.hyperparams.initial_z_var)
            if self.hyperparams.learn_dist:
                self.ln_var = chainer.Parameter(initializer=initial_ln_z_var, shape=[1])
            else:
                self.ln_var = chainer.Variable(initializer=initial_ln_z_var, shape=[1])

            feature_coupling = AdditiveNodeFeatureCoupling if self.hyperparams.additive_feature_coupling else AffineNodeFeatureCoupling
            relation_coupling = AdditiveAdjCoupling if self.hyperparams.additive_relation_coupling else AffineAdjCoupling
            clinks = [
                feature_coupling(self.hyperparams.num_nodes, self.hyperparams.num_edge_types, self.hyperparams.num_features,
                                 self.masks["feature"][i %
                                                       self.hyperparams.num_features],
                                 batch_norm=self.hyperparams.apply_batchnorm, ch_list=self.hyperparams.gnn_channels,
                                 n_attention=self.hyperparams.num_attention_types, gat_layers=self.hyperparams.num_gat_layers)
                for i in range(self.hyperparams.num_coupling["feature"])]
            clinks.extend([
                relation_coupling(self.hyperparams.num_nodes, self.hyperparams.num_edge_types, self.hyperparams.num_features,
                                  self.masks["relation"][i %
                                                         self.hyperparams.num_edge_types],
                                  batch_norm=self.hyperparams.apply_batchnorm, ch_list=self.hyperparams.mlp_channels)
                for i in range(self.hyperparams.num_coupling["relation"])])
            self.clinks = chainer.ChainList(*clinks)

        # load and fix embed model
        chainer.serializers.load_npz(
            self.hyperparams.embed_model_path, self.embed_model)
        self.embed_model.disable_update()
        self.word_channel_stds = self.embed_model.word_channel_stds()

    def __call__(self, x, adj):
        # x (batch_size, ): atom id array
        h = chainer.as_variable(x)
        h = self.embed_model.embedding(h)

        # add gaussian noise
        if chainer.config.train:
            h += (self.xp.random.randn(*h.shape) * self.word_channel_stds * self.hyperparams.feature_noise_scale)

        adj = chainer.as_variable(adj)
        sum_log_det_jacobian_x = chainer.as_variable(
            self.xp.zeros([h.shape[0]], dtype=self.xp.float32))
        sum_log_det_jacobian_adj = chainer.as_variable(
            self.xp.zeros([h.shape[0]], dtype=self.xp.float32))

        # forward step for channel-coupling layers
        for i in range(self.hyperparams.num_coupling["feature"]):
            h, log_det_jacobians = self.clinks[i](h, adj)
            sum_log_det_jacobian_x += log_det_jacobians

        # add uniform noise to adjacency tensors
        if chainer.config.train:
            adj += self.xp.random.uniform(0, 0.9, adj.shape)

        # forward step for adjacency-coupling layers
        for i in range(self.hyperparams.num_coupling["feature"], len(self.clinks)):
            adj, log_det_jacobians = self.clinks[i](adj)
            sum_log_det_jacobian_adj += log_det_jacobians

        adj = F.reshape(adj, (adj.shape[0], -1))
        h = F.reshape(h, (h.shape[0], -1))
        out = [h, adj]
        return out, [sum_log_det_jacobian_x, sum_log_det_jacobian_adj]

    def reverse(self, z, true_adj=None):
        """
        Returns a molecule, given its latent vector.
        :param z: latent vector. Shape: [B, N*N*M + N*T]
            B = Batch size, N = number of atoms, M = number of bond types,
            T = number of atom types (Carbon, Oxygen etc.)
        :param true_adj: used for testing. An adjacency matrix of a real molecule
        :return: adjacency matrix and feature matrix of a molecule
        """
        batch_size = z.shape[0]
        with chainer.no_backprop_mode():
            z_x, z_adj = F.split_axis(chainer.as_variable(z), [self.x_size], 1)

            if true_adj is None:
                h_adj = F.reshape(z_adj, (batch_size, self.hyperparams.num_edge_types,
                                          self.hyperparams.num_nodes, self.hyperparams.num_nodes))

                # First, the adjacency coupling layers are applied in reverse order to get h_adj
                for i in reversed(range(self.hyperparams.num_coupling["feature"], len(self.clinks))):
                    h_adj, _ = self.clinks[i].reverse(h_adj)

                # make adjacency matrix from h_adj
                # 1. make it symmetric
                adj = h_adj + self.xp.transpose(h_adj, (0, 1, 3, 2))
                adj = adj / 2
                # 2. apply normalization along edge type axis and choose the most likely edge type.
                adj = F.softmax(adj, axis=1)
                max_bond = F.broadcast_to(
                    F.max(adj, axis=1, keepdims=True), shape=adj.shape)
                adj = adj // max_bond
            else:
                adj = true_adj

            h_x = F.reshape(
                z_x, (batch_size, self.hyperparams.num_nodes, self.hyperparams.num_features))

            # feature coupling layers
            for i in reversed(range(self.hyperparams.num_coupling["feature"])):
                h_x, _ = self.clinks[i].reverse(h_x, adj)

            atom_ids = self.embed_model.atomid(h_x, adj)

        return atom_ids, adj

    def log_prob(self, z, log_det_jacobians):
        ln_var_adj = self.ln_var * self.xp.ones([self.adj_size])
        ln_var_x = self.ln_var * self.xp.ones([self.x_size])
        log_det_jacobians[0] = log_det_jacobians[0] - F.log(self.xp.array([self.x_size], dtype=self.xp.float32))
        log_det_jacobians[1] = log_det_jacobians[1] - F.log(self.xp.array([self.adj_size], dtype=self.xp.float32))

        negative_log_likelihood_adj = F.average(F.sum(F.gaussian_nll(z[1], self.xp.zeros(
            self.adj_size, dtype=self.xp.float32), ln_var_adj, reduce="no"), axis=1) - log_det_jacobians[1])
        negative_log_likelihood_x = F.average(F.sum(F.gaussian_nll(z[0], self.xp.zeros(
            self.x_size, dtype=self.xp.float32), ln_var_x, reduce="no"), axis=1) - log_det_jacobians[0])

        negative_log_likelihood_adj /= self.adj_size
        negative_log_likelihood_x /= self.x_size

        if negative_log_likelihood_x.array < 0:
            log.warning("negative nll for x!")

        return [negative_log_likelihood_x, negative_log_likelihood_adj]

    def _create_masks(self, channel):
        if channel == "relation":  # for adjacenecy matrix
            return self._simple_masks(self.hyperparams.num_edge_types)
        elif channel == "feature":  # for feature matrix
            return self._simple_masks(self.hyperparams.num_features)

    def _simple_masks(self, N):
        return ~self.xp.eye(N, dtype=self.xp.bool)

    def save_hyperparams(self, path):
        self.hyperparams.save(path)

    def load_hyperparams(self, path):
        self.hyperparams.load(path)

    def load_from(self, path):
        if os.path.exists(path):
            log.info("Try load model from {}".format(path))
            try:
                chainer.serializers.load_npz(path, self)
            except:
                log.warning("Fail in loading model from {}".format(path))
                return False
            return True
        raise ValueError("{} does not exist.".format(path))

    @property
    def z_var(self):
        return F.exp(self.ln_var).array[0]

    def to_gpu(self, device=None):
        super().to_gpu(device=device)
        self.masks["relation"] = chainer.backends.cuda.to_gpu(
            self.masks["relation"], device=device)
        self.masks["feature"] = chainer.backends.cuda.to_gpu(
            self.masks["feature"], device=device)
        self.word_channel_stds = chainer.backends.cuda.to_gpu(
            self.word_channel_stds, device=device)
        for clink in self.clinks:
            clink.to_gpu(device=device)

    def to_cpu(self):
        super().to_cpu()
        self.masks["relation"] = chainer.backends.cuda.to_cpu(
            self.masks["relation"])
        self.masks["feature"] = chainer.backends.cuda.to_cpu(
            self.masks["feature"])
        self.word_channel_stds = chainer.backends.cuda.to_cpu(
            self.word_channel_stds)
        for clink in self.clinks:
            clink.to_cpu()
