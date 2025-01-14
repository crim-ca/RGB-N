# --------------------------------------------------------
# Tensorflow RGB-N
# Licensed under The MIT License [see LICENSE for details]
# Written by Peng Zhou
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import tensorflow.contrib.slim as slim
from tensorflow.contrib.slim import losses
from tensorflow.contrib.slim import arg_scope
from tensorflow.contrib.slim.python.slim.nets import resnet_utils
from tensorflow.contrib.slim.nets import resnet_v1
import numpy as np

from nets.network_fusion import Network
from tensorflow.python.framework import ops
from tensorflow.contrib.layers.python.layers import regularizers
from tensorflow.python.ops import nn_ops
from tensorflow.contrib.layers.python.layers import initializers
from tensorflow.contrib.layers.python.layers import layers
from model.config import cfg
from compact_bilinear_pooling.compact_bilinear_pooling import compact_bilinear_pooling_layer
import pdb

def resnet_arg_scope(is_training=True,
                     weight_decay=cfg.TRAIN.WEIGHT_DECAY,
                     batch_norm_decay=0.997,
                     batch_norm_epsilon=1e-5,
                     batch_norm_scale=True):
  batch_norm_params = {
    # NOTE 'is_training' here does not work because inside resnet it gets reset:
    # https://github.com/tensorflow/models/blob/master/slim/nets/resnet_v1.py#L187
    'is_training': False,
    'decay': batch_norm_decay,
    'epsilon': batch_norm_epsilon,
    'scale': batch_norm_scale,
    'trainable': cfg.RESNET.BN_TRAIN,
    'updates_collections': ops.GraphKeys.UPDATE_OPS
  }

  with arg_scope(
      [slim.conv2d],
      weights_regularizer=regularizers.l2_regularizer(weight_decay),
      weights_initializer=initializers.variance_scaling_initializer(),
      trainable=is_training,
      activation_fn=nn_ops.relu,
      normalizer_fn=layers.batch_norm,
      normalizer_params=batch_norm_params):
    with arg_scope([layers.batch_norm], **batch_norm_params) as arg_sc:
      return arg_sc

class resnet_fusion(Network):
  def __init__(self, batch_size=1, num_layers=50):
    Network.__init__(self, batch_size=batch_size)
    self._num_layers = num_layers
    self._resnet_scope = 'resnet_v1_%d' % num_layers

  def _crop_pool_layer(self, bottom, rois, name):
    with tf.variable_scope(name) as scope:
      batch_ids = tf.squeeze(tf.slice(rois, [0, 0], [-1, 1], name="batch_id"), [1])
      # Get the normalized coordinates of bboxes
      bottom_shape = tf.shape(bottom)
      height = (tf.to_float(bottom_shape[1]) - 1.) * np.float32(self._feat_stride[0])
      width = (tf.to_float(bottom_shape[2]) - 1.) * np.float32(self._feat_stride[0])
      x1 = tf.slice(rois, [0, 1], [-1, 1], name="x1") / width
      y1 = tf.slice(rois, [0, 2], [-1, 1], name="y1") / height
      x2 = tf.slice(rois, [0, 3], [-1, 1], name="x2") / width
      y2 = tf.slice(rois, [0, 4], [-1, 1], name="y2") / height
      # Won't be backpropagated to rois anyway, but to save time
      bboxes = tf.stop_gradient(tf.concat([y1, x1, y2, x2],axis=1))
      if cfg.RESNET.MAX_POOL:
        pre_pool_size = cfg.POOLING_SIZE * 2
        crops = tf.image.crop_and_resize(bottom, bboxes, tf.to_int32(batch_ids), [pre_pool_size, pre_pool_size],
                                         name="crops")
        crops = slim.max_pool2d(crops, [2, 2], padding='SAME')
      else:
        crops = tf.image.crop_and_resize(bottom, bboxes, tf.to_int32(batch_ids), [cfg.POOLING_SIZE, cfg.POOLING_SIZE],
                                         name="crops")
    return crops

  # Do the first few layers manually, because 'SAME' padding can behave inconsistently
  # for images of different sizes: sometimes 0, sometimes 1
  def build_base(self):
    with tf.variable_scope(self._resnet_scope, self._resnet_scope):
      net = resnet_utils.conv2d_same(self._image, 64, 7, stride=2, scope='conv1')
      net = tf.pad(net, [[0, 0], [1, 1], [1, 1], [0, 0]])
      net = slim.max_pool2d(net, [3, 3], stride=2, padding='VALID', scope='pool1')

    return net

  def build_network(self, sess, is_training=True):
    # select initializers
    if cfg.TRAIN.TRUNCATED:
      initializer = tf.truncated_normal_initializer(mean=0.0, stddev=0.01)
      initializer_bbox = tf.truncated_normal_initializer(mean=0.0, stddev=0.001)
    else:
      #initializer = tf.random_normal_initializer(mean=0.0, stddev=0.01)
      initializer = tf.contrib.layers.xavier_initializer()
      initializer_bbox = tf.random_normal_initializer(mean=0.0, stddev=0.001)
    bottleneck = resnet_v1.bottleneck
    # choose different blocks for different number of layers
    if self._num_layers == 50:
      blocks = [
        resnet_utils.Block('block1', bottleneck,
                           [(256, 64, 1)] * 2 + [(256, 64, 2)]),
        resnet_utils.Block('block2', bottleneck,
                           [(512, 128, 1)] * 3 + [(512, 128, 2)]),
        # Use stride-1 for the last conv4 layer
        resnet_utils.Block('block3', bottleneck,
                           [(1024, 256, 1)] * 5 + [(1024, 256, 1)]),
        resnet_utils.Block('block4', bottleneck, [(2048, 512, 1)] * 3)
      ]
    elif self._num_layers == 101:
#      blocks = [
#        resnet_utils.Block('block1', bottleneck,
#                           [(256, 64, 1)] * 2 + [(256, 64, 2)]),
#        resnet_utils.Block('block2', bottleneck,
#                           [(512, 128, 1)] * 3 + [(512, 128, 2)]),
#        # Use stride-1 for the last conv4 layer
#        resnet_utils.Block('block3', bottleneck,
#                           [(1024, 256, 1)] * 22 + [(1024, 256, 1)]),
#        resnet_utils.Block('block4', bottleneck, [(2048, 512, 1)] * 3)
#      ]
      resnet_v1_block = resnet_v1.resnet_v1_block
      blocks = [resnet_v1_block('block1', base_depth=64, num_units=3, stride=2),
                resnet_v1_block('block2', base_depth=128, num_units=4, stride=2),
                resnet_v1_block('block3', base_depth=256, num_units=23, stride=1),
                resnet_v1_block('block4', base_depth=512, num_units=3, stride=1),
               ]
                                                            
    elif self._num_layers == 152:
      blocks = [
        resnet_utils.Block('block1', bottleneck,
                           [(256, 64, 1)] * 2 + [(256, 64, 2)]),
        resnet_utils.Block('block2', bottleneck,
                           [(512, 128, 1)] * 7 + [(512, 128, 2)]),
        # Use stride-1 for the last conv4 layer
        resnet_utils.Block('block3', bottleneck,
                           [(1024, 256, 1)] * 35 + [(1024, 256, 1)]),
        resnet_utils.Block('block4', bottleneck, [(2048, 512, 1)] * 3)
      ]
    else:
      # other numbers are not supported
      raise NotImplementedError

    assert (0 <= cfg.RESNET.FIXED_BLOCKS < 4)
    if cfg.RESNET.FIXED_BLOCKS == 3:
      with slim.arg_scope(resnet_arg_scope(is_training=False)):
        net = self.build_base()
        net_conv4, _ = resnet_v1.resnet_v1(net,
                                           blocks[0:cfg.RESNET.FIXED_BLOCKS],
                                           global_pool=False,
                                           include_root_block=False,
                                           scope=self._resnet_scope)
    elif cfg.RESNET.FIXED_BLOCKS > 0:
      with slim.arg_scope(resnet_arg_scope(is_training=False)):
        net = self.build_base()
        net, _ = resnet_v1.resnet_v1(net,
                                     blocks[0:cfg.RESNET.FIXED_BLOCKS],
                                     global_pool=False,
                                     include_root_block=False,
                                     scope=self._resnet_scope)

      with slim.arg_scope(resnet_arg_scope(is_training=is_training)):
        net_conv4, _ = resnet_v1.resnet_v1(net,
                                           blocks[cfg.RESNET.FIXED_BLOCKS:-1],
                                           global_pool=False,
                                           include_root_block=False,
                                           scope=self._resnet_scope)
    else:  # cfg.RESNET.FIXED_BLOCKS == 0
      with slim.arg_scope(resnet_arg_scope(is_training=is_training)):
        net = self.build_base()
        net_conv4, _ = resnet_v1.resnet_v1(net,
                                           blocks[0:-1],
                                           global_pool=False,
                                           include_root_block=False,
                                           scope=self._resnet_scope)
    self._act_summaries.append(net_conv4)
    self._layers['head'] = net_conv4
    c=np.zeros((3,5,5))
    c[0]=[[-1,2,-2,2,-1],[2,-6,8,-6,2],[-2,8,-12,8,-2],[2,-6,8,-6,2],[-1,2,-2,2,-1]]
    c[0]=c[0]/12

    c[1][1][1]=-1
    c[1][1][2]=2
    c[1][1][3]=-1
    c[1][2][1]=2
    c[1][2][2]=-4
    c[1][2][3]=2
    c[1][3][1]=-1
    c[1][3][2]=2
    c[1][3][3]=-1
    c[1]=c[1]/4

    c[2][2][1]=1
    c[2][2][2]=-2
    c[2][2][3]=1
    c[2]=c[2]/2

    Wcnn=np.zeros((5,5,3,3))
    for i in range(3):
      #k=i%10+1
      #Wcnn[i]=[c[3*k-3],c[3*k-2],c[3*k-1]]
      Wcnn[:,:,0,i]=c[i]
      Wcnn[:,:,1,i]=c[i]
      Wcnn[:,:,2,i]=c[i]
    if True:
      with tf.variable_scope('noise'):
        #kernel = tf.get_variable('weights',
                              #shape=[5, 5, 3, 3],
                              #initializer=tf.constant_initializer(c))
        convx= tf.nn.conv2d(self.noise, Wcnn, [1, 1, 1, 1], padding='SAME',name='srm')
        conv=tf.Print(convx,[tf.shape(convx)],message="xx",summarize=-1)

        # add self-consistency img as 2nd channel
        #conv=tf.concat([conv,self._scimage],axis=3)


      self._layers['noise']=conv
      with slim.arg_scope(resnet_arg_scope(is_training=is_training)):
        #srm_conv = tf.nn.tanh(conv, name='tanh')
        noise_netx = resnet_utils.conv2d_same(conv, 64, 7, stride=2, scope='conv1')
        noise_net = tf.Print(noise_netx, [tf.shape(noise_netx)], message="noisenet")
        noise_nety = tf.pad(noise_net, [[0, 0], [1, 1], [1, 1], [0, 0]])
        noise_net = tf.Print(noise_nety, [tf.shape(noise_nety)], message="noisenetafterpad")
        noise_netz = slim.max_pool2d(noise_net, [3, 3], stride=2, padding='VALID', scope='pool1')
        noise_net = tf.Print(noise_netz, [tf.shape(noise_netz)], message="noisenetaftermaxpool")
        #net_sum=tf.concat(3,[net_conv4,noise_net])
        noise_conv4, _ = resnet_v1.resnet_v1(noise_net,
                                           blocks[0:-1],
                                           global_pool=False,
                                           include_root_block=False,
                                           scope='noise')
    with tf.variable_scope(self._resnet_scope, self._resnet_scope):
      # build the anchors for the image
      self._anchor_component()

      # rpn
      rpn = slim.conv2d(net_conv4, 512, [3, 3], trainable=is_training, weights_initializer=initializer,
                        scope="rpn_conv/3x3")
      self._act_summaries.append(rpn)

      # e.g. self._num_anchors = 12

      rpn_cls_score = slim.conv2d(rpn, self._num_anchors * 2, [1, 1], trainable=is_training,     # x2 because of bg/fg ?
                                  weights_initializer=initializer,
                                  padding='VALID', activation_fn=None, scope='rpn_cls_score')
      # change it so that the score has 2 as its channel size
      print(rpn_cls_score)
      rpn_cls_score_reshape = self._reshape_layer(rpn_cls_score, 2, 'rpn_cls_score_reshape')
      rpn_cls_prob_reshape = self._softmax_layer(rpn_cls_score_reshape, "rpn_cls_prob_reshape")
      rpn_cls_prob = self._reshape_layer(rpn_cls_prob_reshape, self._num_anchors * 2, "rpn_cls_prob")
      rpn_bbox_pred = slim.conv2d(rpn, self._num_anchors * 4, [1, 1], trainable=is_training,
                                  weights_initializer=initializer,
                                  padding='VALID', activation_fn=None, scope='rpn_bbox_pred')
      if is_training:
        rois, roi_scores = self._proposal_layer(rpn_cls_prob, rpn_bbox_pred, "rois")
        rpn_labels = self._anchor_target_layer(rpn_cls_score, "anchor")
        # Try to have a determinestic order for the computing graph, for reproducibility
        with tf.control_dependencies([rpn_labels]):
          rois, _ = self._proposal_target_layer(rois, roi_scores, "rpn_rois")
      else:
        if cfg.TEST.MODE == 'nms':
          rois, _ = self._proposal_layer(rpn_cls_prob, rpn_bbox_pred, "rois")
        elif cfg.TEST.MODE == 'top':
          rois, _ = self._proposal_top_layer(rpn_cls_prob, rpn_bbox_pred, "rois")
        else:
          raise NotImplementedError
      # rcnn
      if cfg.POOLING_MODE == 'crop':
        pool5 = self._crop_pool_layer(net_conv4, rois, "pool5")
        #pool5 = self._crop_pool_layer(net_sum, rois, "pool5")
      else:
        raise NotImplementedError
    if True:
      noise_pool5 = self._crop_pool_layer(noise_conv4, rois, "noise_pool5")
      with slim.arg_scope(resnet_arg_scope(is_training=is_training)):
        noise_fc7, _ = resnet_v1.resnet_v1(noise_pool5,
                                   blocks[-1:],
                                   global_pool=False,
                                   include_root_block=False,
                                   scope='noise')
    with slim.arg_scope(resnet_arg_scope(is_training=is_training)):
      fc7, _ = resnet_v1.resnet_v1(pool5,
                                   blocks[-1:],
                                   global_pool=False,
                                   include_root_block=False,
                                   scope=self._resnet_scope)
    self._layers['fc7']=fc7
    with tf.variable_scope('noise_pred'):

      bilinear_pool=compact_bilinear_pooling_layer(fc7,noise_fc7,2048*8,compute_size=16,sequential=False)
      fc7=tf.Print(fc7,[tf.shape(fc7)],message='Value of %s' % 'fc', summarize=4, first_n=1)
      bilinear_pool=tf.reshape(bilinear_pool, [-1,2048*8])
      bilinear_pool=tf.Print(bilinear_pool,[tf.shape(bilinear_pool)],message='Value of %s' % 'Blinear', summarize=4, first_n=1)
      bilinear_pool=tf.multiply(tf.sign(bilinear_pool),tf.sqrt(tf.abs(bilinear_pool)+1e-12))
      bilinear_pool=tf.nn.l2_normalize(bilinear_pool,dim=1)
      noise_cls_score = slim.fully_connected(bilinear_pool, self._num_classes, weights_initializer=tf.contrib.layers.xavier_initializer(),
                                       trainable=is_training, activation_fn=None, scope='cls_score')
      cls_prob = self._softmax_layer(noise_cls_score, "cls_prob")
      fc7 = tf.reduce_mean(fc7, axis=[1, 2])    




      bbox_pred = slim.fully_connected(fc7, self._num_classes * 4, weights_initializer=initializer_bbox,
                                     trainable=is_training,
                                     activation_fn=None, scope='bbox_pred')

    self._predictions["rpn_cls_score"] = rpn_cls_score
    self._predictions["rpn_cls_score_reshape"] = rpn_cls_score_reshape
    self._predictions["rpn_cls_prob"] = rpn_cls_prob
    self._predictions["rpn_bbox_pred"] = rpn_bbox_pred
    self._predictions["cls_score"] = noise_cls_score
    self._predictions["cls_prob"] = cls_prob
    self._predictions["bbox_pred"] = bbox_pred
    self._predictions["rois"] = rois

    self._score_summaries.update(self._predictions)

    return rois, cls_prob,bbox_pred

  def get_variables_to_restore(self, variables, var_keep_dic):
    variables_to_restore = []

    for v in variables:
      # exclude the first conv layer to swap RGB to BGR
      if v.name == (self._resnet_scope + '/conv1/weights:0'):
        self._variables_to_fix[v.name] = v
        continue
      elif v.name.split('/')[0]=='noise_pred':
        continue
      if v.name.split(':')[0] in var_keep_dic:
        print('Varibles restored: %s' % v.name)
        variables_to_restore.append(v)

    return variables_to_restore

  def fix_variables(self, sess, pretrained_model):
    print('not Fix Resnet V1 layers..')
    with tf.variable_scope('Fix_Resnet_V1') as scope:
      with tf.device("/cpu:0"):
        # fix RGB to BGR
        conv1_rgb = tf.get_variable("conv1_rgb", [7, 7, 3, 64], trainable=False)
        restorer_fc = tf.train.Saver({self._resnet_scope + "/conv1/weights": conv1_rgb})
        restorer_fc.restore(sess, pretrained_model)

        sess.run(tf.assign(self._variables_to_fix[self._resnet_scope + '/conv1/weights:0'], 
                           tf.reverse(conv1_rgb, [2]))) #[False,False,True,False])))
