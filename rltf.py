import tensorflow as tf, numpy as np
from tensorflow.contrib import rnn
import sys, os, multiprocessing, time, math
from utils import *
from tflayers.mhdpa import *
from tflayers.tfutils import *
from tflayers.inst_gradients import *
import er

flags = tf.app.flags
flags.DEFINE_integer('inst', 0, 'ID of agent that accumulates gradients on server')
flags.DEFINE_integer('seq_keep', 0, 'Sequences recorded from user actions')
flags.DEFINE_integer('seq_inst', 128, 'Sequences in queue recorded from agents')
flags.DEFINE_integer('seq_per_inst', 64, 'Sequences recorded per agent instance')
flags.DEFINE_integer('minibatch', 64, 'Minibatch size')
flags.DEFINE_integer('update_mb', 10, 'Minibatches per policy update')
flags.DEFINE_boolean('replay', False, 'Replay actions recorded in memmap array')
flags.DEFINE_boolean('recreate_states', False, 'Recreate kept states from saved raw frames')
flags.DEFINE_boolean('record', False, 'Record over kept sequences')
flags.DEFINE_string('summary', '/tmp/tf', 'Summaries path for Tensorboard')
flags.DEFINE_string('env_seed', '', 'Seed number for new environment')
flags.DEFINE_float('sample_action', 0., 'Sample actions, or use modal policy')
flags.DEFINE_float('learning_rate', 1e-3, 'Learning rate')
flags.DEFINE_float('gamma', 0.99, 'Discount rate')
flags.DEFINE_integer('nsteps', 30, 'Multi-step returns for Q-function')
FLAGS = flags.FLAGS

PORT, PROTOCOL = 'localhost:2222', 'grpc'
if not FLAGS.inst:
    server = tf.train.Server({'local': [PORT]}, protocol=PROTOCOL, start=True)
sess = tf.InteractiveSession(PROTOCOL+'://'+PORT)

LSTM_UNROLL = 0
MHDPA_LAYERS = 0#3
FC_UNITS = 256

FIXED_ACTIONS = []
ENV_NAME = os.getenv('ENV')
er.ENV_NAME = ENV_NAME
if not ENV_NAME:
    raise Exception('Missing ENV environment variable')
if ENV_NAME == 'CarRacing-v0':
    import gym.envs.box2d
    car_racing = gym.envs.box2d.car_racing
    car_racing.WINDOW_W = 800 # Default is huge
    car_racing.WINDOW_H = 600
    FIXED_ACTIONS = [
        [-1., 0., 0.],
        [1., 0., 0.],
        [0., 1., 0.],
        [0., 0., 1.],
        [0., 0., 0.]]
elif ENV_NAME == 'MountainCarContinuous-v0':
    FIXED_ACTIONS = [[-1.], [1.]]
elif ENV_NAME == 'FlappyBird-v0':
    import gym_ple # [512, 288]
elif 'Bullet' in ENV_NAME:
    import pybullet_envs

import gym
env = gym.make(ENV_NAME)
env._max_episode_steps = None # Disable step limit
envu = env.unwrapped

from pyglet import gl
def draw_line(a, b, color=(1,1,1), alpha=1.):
    gl.glLineWidth(3)
    gl.glBegin(gl.GL_LINES)
    gl.glColor4f(*(color+[0.]))
    gl.glVertex3f(window.width*a[0], window.height*(1-a[1]), 0)
    gl.glColor4f(*(color+[alpha]))
    gl.glVertex3f(window.width*b[0], window.height*(1-b[1]), 0)
    gl.glEnd()
def draw_attention():
    if not app.draw_attention or not MHDPA_LAYERS:
        return
    s = app.per_inst.attention.shape[1:]
    for head in range(3):
        color = onehot_vector(head, 3)
        for y1 in range(s[0]):
            for x1 in range(s[1]):
                for a in range(s[2]):
                    f = app.per_inst.attention[0, y1,x1, a, head]
                    if f < 0.1: continue

                    idx = app.per_inst.top_k_idx[0][a]
                    y2, x2 = idx/s[0], idx % s[0]
                    draw_line(
                        ((x1+0.5)/s[1], (y1+0.5)/s[0]),
                        ((x2+0.5)/s[1], (y2+0.5)/s[0]),
                        color, f)
def hook_swapbuffers():
    flip = window.flip
    def hook():
        draw_attention()
        flip()
    window.flip = hook

ACTION_DIMS = (env.action_space.shape or [env.action_space.n])[0]
ACTION_DISCRETE = not env.action_space.shape
def onehot_vector(idx, dims): return [1. if idx == i else 0. for i in range(dims)]
if ACTION_DISCRETE:
    MULTI_ACTIONS = [onehot_vector(a, ACTION_DIMS) for a in range(ACTION_DIMS)]
else:
    MULTI_ACTIONS = []
    ACTION_TANH = range(ACTION_DIMS)
    if ENV_NAME == 'CarRacing-v0':
        ACTION_TANH = [0]
    ACTION_CLIP = [-1. if i in ACTION_TANH else 0. for i in range(ACTION_DIMS)], [1.]*ACTION_DIMS
MULTI_ACTIONS = tf.constant(MULTI_ACTIONS, DTYPE)
POLICY_SOFTMAX = ACTION_DISCRETE
POLICY_BETA = False
FRAME_DIM = list(env.observation_space.shape)

CONV_NET = len(FRAME_DIM) == 3
STATE_DIM = FRAME_DIM[:]
if CONV_NET:
    FRAME_LCN = False
    GRAYSCALE = True
    if GRAYSCALE and STATE_DIM[-1] == 3:
        STATE_DIM[-1] = 1
    CHANNELS = STATE_DIM[-1]

    RESIZE = [84, 84]
    if RESIZE:
        STATE_DIM[:2] = RESIZE
STATE_DIM[-1] *= er.ACTION_REPEAT

training = Struct(enable=True, seq_recorded=0, batches_mtime={}, temp_batch=None)
ph = Struct(
    actions=tf.placeholder(DTYPE, [FLAGS.minibatch, ACTION_DIMS]),
    states=tf.placeholder(DTYPE, [FLAGS.minibatch, er.CONCAT_STATES] + STATE_DIM),
    rewards=tf.placeholder(DTYPE, [FLAGS.minibatch, er.ER_REWARDS]),
    frame=tf.placeholder(DTYPE, [er.CONCAT_STATES, er.ACTION_REPEAT] + FRAME_DIM),
    mb_count=tf.placeholder('int32', []),
    inst_explore=tf.placeholder(DTYPE, [ACTION_DIMS]))

if CONV_NET:
    frame_to_state = tf.reshape(ph.frame, [-1] + FRAME_DIM) # Combine CONCAT_STATES and er.ACTION_REPEAT
    if RESIZE:
        frame_to_state = tf.image.resize_images(frame_to_state, RESIZE, tf.image.ResizeMethod.AREA)
    if FRAME_LCN:
        frame_to_state = local_contrast_norm(frame_to_state, GAUSS_W)
        frame_to_state = tf.reduce_max(frame_to_state, axis=-1)
    else:
        if GRAYSCALE: frame_to_state = tf.reduce_mean(frame_to_state, axis=-1, keep_dims=True)
        frame_to_state = frame_to_state/255.
    frame_to_state = tf.reshape(frame_to_state, [er.CONCAT_STATES, er.ACTION_REPEAT] + RESIZE)
    frame_to_state = tf.transpose(frame_to_state, [0, 2, 3, 1])# Move er.ACTION_REPEAT into channels
else:
    frame_to_state = tf.reshape(ph.frame, [er.CONCAT_STATES] + STATE_DIM)

app = Struct(policy_index=0, quit=False, mb_count=0, print_action=FLAGS.inst==1, show_state_image=False,
    draw_attention=False, wireframe=True, pause=False)
if FLAGS.inst:
    FIRST_SEQ = (FLAGS.inst-1)*FLAGS.seq_per_inst
else:
    FIRST_SEQ = -FLAGS.seq_keep
    def init_vars(): sess.run(tf.global_variables_initializer())
    if FLAGS.record or FLAGS.replay or FLAGS.recreate_states:
        # Record new arrays
        app.policy_index = -1
    else:
        training.seq_recorded = FLAGS.seq_keep
training.append_batch = FIRST_SEQ

ops = Struct(
    per_mb=[], post_mb=[],
    per_update=[], post_update=[],
    new_batches=[],
    per_inst=[frame_to_state], post_inst=[])

def apply_gradients(grads, opt, clip_norm=100.):
    grads, weights = zip(*grads)
    clipped,global_norm = tf.clip_by_global_norm(grads, clip_norm)
    if clip_norm:
        grads = clipped
    grads = zip(grads, weights)
    ops.per_mb.append(opt.apply_gradients(grads))
    return global_norm

def queue_push(value, shift_queue=False, queue_size=FLAGS.nsteps):
    queue = tf.Variable(tf.zeros([queue_size] + value.shape.as_list()), trainable=False)
    if shift_queue:
        # op[0] is oldest value, and the next to be discarded
        new_queue_value = tf.concat([queue[1:], [value]], 0)
        op = queue.assign(new_queue_value)
        ops.per_mb.append(op)
        ops.new_batches.append(queue.assign(tf.zeros_like(queue)))
        return new_queue_value
    else:
        op = tf.scatter_update(queue, tf.mod(ph.mb_count-1, FLAGS.nsteps), value)
        op = op[tf.mod(ph.mb_count, FLAGS.nsteps)]
        return op

def calc_gradients(cost, mult, weights):
    grads = inst_gradients(cost, weights)
    grads = [(queue_push(g),w) for g,w in grads]
    return inst_gradients_multiply(grads, mult)

INST_0 = 0
MB_1 = MB_BN = 1
def batch_norm_gen(norm_groups=0, activation=tf.nn.relu):
    norm = tf.layers.BatchNormalization(scale=False, center=False, momentum=0.1)
    def ret(n, it):
        if norm_groups:
            n = tf.reshape(n, n.shape.as_list()[:-1] + [n.shape[-1]/norm_groups, norm_groups])

        training = it != INST_0
        n = norm.apply(n, training)
        for w in norm.weights: variable_summaries(w)

        if norm_groups:
            n = tf.reshape(n, n.shape.as_list()[:-2] + [-1])
        if activation: n = activation(n)
        return n
    it = 0
    while True:
        yield lambda n, it=it: ret(n, it)
        it += 1
def layer_batch_norm(x, *args):
    return [g(n) for n,g in zip(x, batch_norm_gen(*args))]

def layer_dense(x, outputs, activation=None, use_bias=False, trainable=True):
    dense = tf.layers.Dense(outputs, activation, use_bias, trainable=trainable)
    x = [apply_layer(dense, [n]) for n in x]
    for w in dense.weights: variable_summaries(w)
    return x

def context_get(caller, create_ctx, i):
    scope = tf.get_variable_scope().name.replace('/new/', '/old/') + str(i)
    if 'contexts' not in caller.__dict__:
        caller.contexts = {}
    context_vars = caller.contexts.get(scope)
    if not context_vars:
        with tf.variable_scope(str(FLAGS.inst)):
            context_vars = create_ctx(i)
            sess.run(tf.variables_initializer(context_vars))
            caller.contexts[scope] = context_vars
    return context_vars

def context_save(caller, context_vars, final_state, i):
    with tf.control_dependencies(final_state):
        [ops.per_inst, ops.per_mb][i] += [context_vars[c].assign(final_state[c]) for c in range(len(context_vars))]

def layer_lstm(x, outputs):
    batch_norm = batch_norm_gen(None)
    cell = LSTMCellBN(outputs)
    #cell = rnn.LSTMCell(outputs)
    x = x[:]

    with tf.variable_scope('lstm'):
        def create_ctx(i):
            return [tf.Variable(tf.zeros((x[i].shape[0], outputs)), trainable=False, collections=[None]) for c in range(2)]
        context = context_get(layer_lstm, create_ctx, INST_0)
        cell.batch_norm = next(batch_norm)
        x[INST_0], final_state = apply_layer(cell, [x[INST_0], context], extra_objs=['_linear1'])
        context_save(layer_lstm, context, final_state, INST_0)
        if FLAGS.inst:
            return x

        x[MB_1] = queue_push(x[MB_1], True, LSTM_UNROLL)
        cell.batch_norm = next(batch_norm)
        context = None
        for u in range(LSTM_UNROLL):
            n, context = apply_layer(cell, [x[MB_1][u], context], extra_objs=['_linear1'])
        x[MB_1] = n

    for w in cell.weights: variable_summaries(w)
    return x

def make_fc(x):
    with tf.variable_scope('fc'):
        if LSTM_UNROLL: x = layer_lstm(x, FC_UNITS)
        else:
            x = layer_dense(x, FC_UNITS)
            x = layer_batch_norm(x, 32)
    return x

def self_attention_layer(x, l, last_layer):
    if not MHDPA_LAYERS: return x
    with tf.variable_scope('mhdpa_%i' % l):
        top_k, top_k_idx = zip(*[top_k_conv(n, 30) for n in x])
        mhdpa = MHDPA()
        A, attention = zip(*[apply_layer(mhdpa, [n, k, not last_layer]) for n,k in zip(x, top_k)])
        if last_layer: # Max pool entire image
            x = A
            for i in range(2):
                x = [tf.reduce_max(n, 1) for n in x]
        else:
            A = layer_batch_norm(A)
            x = [n+a for n,a in zip(x,A)]

        if not FLAGS.inst:
            ac.per_mb.__dict__['attention_minmax_%i'%l] = tf.stack([tf.reduce_min(attention[MB_1]), tf.reduce_max(attention[MB_1])])
        if last_layer:
            ac.per_inst.top_k_idx = top_k_idx[INST_0]
            ac.per_inst.attention = attention[INST_0] # Display agent attention
        return x

def make_conv_net(x):
    LAYERS = [
        (32, 8, 2, 0),
        (32, 8, 2, 0),
        (32, 8, 1, 0),
    ]
    x = [tf.expand_dims(n,-1) for n in x]
    for l,(filters, width, stride, conv3d) in enumerate(LAYERS):
        with tf.variable_scope('conv_%i' % l):
            kwds = dict(activation=None, use_bias=False)
            if conv3d:
                width3d, stride3d = conv3d
                conv = tf.layers.Conv3D(filters, (width, width, width3d), (stride, stride, stride3d), **kwds)
            else:
                if len(x[0].shape) == 5: # Back to 2D conv
                    x = [tf.reshape(n, n.shape.as_list()[:3] + [-1]) for n in x]
                conv = tf.layers.Conv2D(filters, width, stride, **kwds)
            x = [apply_layer(conv, [n]) for n in x]
            for w in conv.weights: variable_summaries(w)
            x = layer_batch_norm(x)
            print(x[0].shape)

    #x = self_attention_layer(x, 2, False); print(x[0].shape)
    x = self_attention_layer(x, 3, True)
    return x

def make_shared():
    if not make_shared.inputs:
        # Move concat states to last dimension
        state_inputs = [tf.expand_dims(frame_to_state,0)] + ([] if FLAGS.inst else [ph.states])
        idx = range(len(state_inputs[0].shape))
        idx = [0] + idx[2:]
        idx.insert(-1, 1)
        CONCAT_STATE_DIM = STATE_DIM[:]
        CONCAT_STATE_DIM[-1] *= er.CONCAT_STATES
        state_inputs = [tf.reshape(tf.transpose(n,idx), [-1]+CONCAT_STATE_DIM) for n in state_inputs]
        if not FLAGS.inst:
            if MB_BN != MB_1: state_inputs.insert(MB_BN, state_inputs[MB_1])
        make_shared.inputs = state_inputs
    x = make_shared.inputs

    print(x[0].shape)
    if CONV_NET: x = make_conv_net(x)
    else: x = tile_tensors(x)

    x = [tf.layers.flatten(n) for n in x]
    print(x[0].shape)
    return Struct(layers=x, weights=scope_vars())
make_shared.inputs = None

POLICY_OPTIONS = max(1, len(FIXED_ACTIONS))
def make_policy_dist(hidden, iteration):
    MIN_STD = 0.1 # Unstable policy gradient for very small stddev

    if FIXED_ACTIONS:
        return [tf.distributions.Normal([FIXED_ACTIONS[iteration]], MIN_STD) for n in hidden]
    if POLICY_OPTIONS > 1:
        hidden = [tf.ones_like(n[:,:1]) for n in hidden]

    def layer(scope, activation, outputs=ACTION_DIMS):
        with tf.variable_scope(scope):
            return layer_dense(hidden, outputs, activation)

    with tf.variable_scope('output_%i' % iteration):
        if POLICY_SOFTMAX:
            return layer('logits', tf.distributions.Categorical)

        if POLICY_BETA: return [tf.distributions.Beta(1.+a, 1.+b) for a,b in
            zip(layer('alpha', tf.nn.softplus), layer('beta', tf.nn.softplus))]

        mean = layer('mean', None)
        return [tf.distributions.Normal(m, MIN_STD#) for m in mean]
            +s) for m,s in zip(mean, layer('stddev', tf.nn.softplus))]

def make_policy(shared):
    hidden = make_fc(shared.layers)
    rng = range(len(hidden))

    policy = zip(*[make_policy_dist(hidden, i) for i in range(POLICY_OPTIONS)])
    if POLICY_OPTIONS > 1:
        with tf.variable_scope('choice'):
            choice_dist = [tf.distributions.Categorical(n) for n in layer_dense(hidden, POLICY_OPTIONS)]
        sample_idx = INST_0
        choice = [tf.one_hot(c.sample() if i==sample_idx else c.mode(), POLICY_OPTIONS) for i,c in enumerate(choice_dist)]
        choice_logits = [n.logits for n in choice_dist]
    else:
        choice = choice_logits = [tf.ones(shape=[n.shape[0], 1]) for n in hidden]
    ret = Struct(choice=choice, weights=scope_vars()+shared.weights)

    sub_mode = [tf.stack([p.mode() for p in policy[i]], 1) for i in rng]
    sub_sample = [tf.stack([p.sample() for p in policy[i]], 1) for i in rng]
    choice_expand = ret.choice if POLICY_SOFTMAX else [tf.expand_dims(c,-1) for c in ret.choice]
    mode = [tf.reduce_sum(c*m, 1) for c,m in zip(choice_expand, sub_mode)]
    sample = [tf.reduce_sum(c*s, 1) for c,s in zip(choice_expand, sub_sample)]

    ret.inst = Struct(mode=sub_mode[INST_0], sample=sub_sample[INST_0],
        choice_softmax=tf.nn.softmax(choice_logits[INST_0]))
    if FLAGS.inst:
        return ret

    if POLICY_SOFTMAX: ph_actions = tf.arg_max(ph.actions, -1)
    elif POLICY_BETA:
        ph_actions = (ph.actions - ACTION_CLIP[0]) / (np.array(ACTION_CLIP[1])-np.array(ACTION_CLIP[0]))
        ph_actions = tf.clip_by_value(ph_actions, 0.01, 0.99)
    else: ph_actions = ph.actions

    ret.mb = Struct(mode=mode[MB_1], choice_softmax=tf.nn.softmax(choice_logits[MB_1]))
    log_softmax = tf.log(tf.minimum(0.98, ret.mb.choice_softmax))
    def action_log_prob(actions):
        log_prob_sub = tf.stack([p.log_prob(actions(p)) for p in policy[MB_1]], -1)
        if not POLICY_SOFTMAX:
            log_prob_sub = tf.reduce_sum(log_prob_sub, 1) # Multiply action axes together
        log_prob = log_prob_sub + log_softmax
        return tf.reduce_logsumexp(log_prob, -1), log_prob_sub

    logsumexp, log_prob_sub = action_log_prob(lambda _: ph_actions)
    peak_logsumexp, _ = action_log_prob(lambda p: p.mode())
    ret.importance_ratio = tf.exp(logsumexp-peak_logsumexp)

    ret.log_prob = tf.reduce_sum(
        tf.stop_gradient(tf.nn.softmax(log_prob_sub)) * log_softmax +
        tf.stop_gradient(ret.mb.choice_softmax) * log_prob_sub, -1)
    return ret

def tile_tensors(x, tiles=10):
    x = [tf.concat([n*tiles + t for t in range(-tiles+1,tiles)], -1) for n in x]
    x = [tf.clip_by_value(n, -1., 1.) for n in x]
    return x

range_steps = tf.expand_dims(tf.range(FLAGS.nsteps), -1)
def make_qvalue(shared):
    combined = make_fc(shared.layers)
    with tf.variable_scope('output'):
        q = layer_dense(combined, 1)

    ret = Struct(q=q, weights=shared.weights + scope_vars(), q_inst=q[INST_0])
    if FLAGS.inst: return ret
    ret.q_mb = q[MB_1][:,0]
    ret.state_values = queue_push(ret.q_mb, True)

    with tf.variable_scope('error_value'):
        error_predict = layer_dense(combined, 1)[MB_1][:,0]
        error_weights = scope_vars()

    def update_qvalue(target_value, importance_ratio):
        td_error = target_value - ret.state_values[0]

        grads = calc_gradients(error_predict, (td_error-error_predict) * importance_ratio, error_weights)
        gnorm_error = apply_gradients(grads, opt.error)

        print('VALUE weights')
        for w in ret.weights: print(w.name, w.shape.as_list())
        grad_s = calc_gradients(ret.q_mb, td_error * importance_ratio, ret.weights)
        if 0:
            grad_s2 = calc_gradients(target_value, -error_predict * importance_ratio, ret.weights)
            for i in range(len(grad_s)):
                (g, w), g2 = grad_s[i], grad_s2[i][0]
                grad_s[i] = (g+g2, w)
        gnorm = apply_gradients(grad_s, opt.td)

        ac.per_mb.gnorm_error = gnorm_error
        ac.per_mb.gnorm_qvalue = gnorm
        return td_error

    ret.update = update_qvalue
    return ret

opt = tf.train.AdamOptimizer
opt = Struct(td=opt(FLAGS.learning_rate),
    policy=opt(FLAGS.learning_rate/10),
    error=opt(1))

def copy_weights(_to, _from, lerp=1.):
    assert(len(_to) == len(_from))
    copy_ops = [to.assign(lerp*w + (1-lerp)*to) for to,w in zip(_to, _from)]
    if lerp != 1.:
        with tf.control_dependencies(copy_ops):
            copy_ops += copy_weights(_from, _to)
    return copy_ops

allac = []
def make_acrl():
    global ac
    ac = Struct(per_mb=Struct(), per_update=Struct(), per_inst=Struct())
    allac.append(ac)

    def _value(shared):
        shared = shared or make_shared()
        with tf.variable_scope('mean'): return make_qvalue(shared)
    def _policy(shared):
        shared = shared or make_shared()
        with tf.variable_scope('_'): return make_policy(shared)

    def make_networks():
        shared = make_shared() if 0 else None
        with tf.variable_scope('policy'): policy = _policy(shared)
        with tf.variable_scope('value'): value = _value(shared)
        ret = Struct(policy=policy, value=value, weights=scope_vars() + (shared.weights if shared else[]))
        return ret

    with tf.variable_scope('old'):
        old = make_networks()

    ac.per_inst.policy_mode = old.policy.inst.mode[0]
    ac.per_inst.policy_sample = old.policy.inst.sample[0]
    ac.per_inst.choice_softmax = old.policy.inst.choice_softmax[0]
    ac.per_inst.policy_value = old.value.q_inst[0]
    if FLAGS.inst: return

    with tf.variable_scope('new'):
        new = make_networks()
    ops.post_update += copy_weights(old.weights, new.weights)

    seq_len = er.TRAJECTORY_LENGTH
    queue_len = seq_len - tf.mod(ph.mb_count-FLAGS.nsteps+1, seq_len)

    rewards = queue_push(ph.rewards[:,r], True)
    state_values = old.value.state_values

    gamma_steps = FLAGS.gamma**tf.cast(range_steps, tf.float32)
    nstep_return = state_values*gamma_steps +\
        tf.concat([tf.zeros_like(rewards[:1]), (tf.cumsum(rewards)*gamma_steps)[:-1]], 0)
    # Check nsteps from the same sequence as current state
    nstep_return = tf.where(tf.tile(range_steps<queue_len, [1, FLAGS.minibatch]), nstep_return,
        tf.tile([tf.gather(nstep_return, tf.minimum(FLAGS.nsteps, queue_len)-1)], [FLAGS.nsteps, 1]))[1:]

    return_value = nstep_return[-1]
    target_value = nstep_return[-1]

    importance_ratio = queue_push(old.policy.importance_ratio)
    td_error = new.value.update(target_value, importance_ratio)

    adv = tf.stop_gradient(return_value - state_values[0])
    adv = tf.maximum(0., adv) # Only towards better actions
    adv_unscaled = adv

    # Normalize the advantages
    #adv /= tf.reduce_sum(adv) + 1e-8

    policy_w = new.policy.weights
    print('POLICY weights:')
    for w in policy_w: print(w.name, w.shape.as_list())
    policy_w_output = [w for w in policy_w if 'output_' in w.name]
    policy_w = [w for w in policy_w if w not in policy_w_output]

    policy_ratio = new.policy.log_prob - old.policy.log_prob # PPO objective
    cliprange = 0.2
    if 1:
        policy_ratio = tf.exp(tf.minimum(1., policy_ratio))
        cliprange += 1.
    ratio = queue_push(policy_ratio)

    adv = tf.where(ratio < cliprange, adv, tf.zeros_like(adv))
    grads_adv = calc_gradients(policy_ratio, adv, policy_w + policy_w_output)

    ac.per_mb.gnorm_policy_adv = apply_gradients(grads_adv[:len(policy_w)], opt.policy)
    if policy_w_output:          apply_gradients(grads_adv[len(policy_w):], opt.policy)

    maxmin = lambda n: (tf.reduce_max(n, 0), tf.reduce_min(n, 0))
    stats = ac.per_mb
    stats.mb_action_max, stats.mb_action_min = maxmin(ph.actions)
    stats.mb_reward_sum = tf.reduce_sum(ph.rewards[:,r])
    stats.policy_max, stats.policy_min = maxmin(old.policy.mb.mode)
    abs_error = tf.abs(td_error)
    stats.abs_error_sum = tf.reduce_sum(abs_error)
    stats.policy_ratio_max, _ = maxmin(policy_ratio)
    stats.policy_ratio_mean = tf.reduce_mean(policy_ratio, 0)
    stats.importance_ratio_max, stats.importance_ratio_min = maxmin(importance_ratio)
    stats.log_prob_max, stats.log_prob_min = maxmin(old.policy.log_prob)
    stats.choice_softmax_max, _ = maxmin(old.policy.mb.choice_softmax)
    stats.priority = abs_error + adv_unscaled

for r in range(er.ER_REWARDS):
    with tf.variable_scope('ac'): make_acrl()

state = Struct(frames=np.zeros([er.CONCAT_STATES, er.ACTION_REPEAT] + FRAME_DIM),
               count=0, last_obs=None, last_pos_reward=0,
               done=True, next_reset=False, last_reset=0,
               ph_attention=None)

def env_render():
    lines = lambda l: gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE if l else gl.GL_FILL)
    lines(app.wireframe)
    env.render()
    lines(False)

def setup_key_actions():
    from pyglet.window import key
    a = np.array([0.]*max(3, ACTION_DIMS))

    def settings_caption():
        d = dict(inst=FLAGS.inst,
            policy_index=app.policy_index,
            options=(['sample '+str(FLAGS.sample_action)] if FLAGS.sample_action else []) +
                (['print'] if app.print_action else []) +
                (['attention'] if app.draw_attention else []) +
                (['pause'] if app.pause else []))
        print(d)
        window.set_caption(str(d))

    on_close = lambda: setattr(app, 'quit', True)
    def key_press(k, mod):
        if k==key.LEFT:  a[0] = -1.0
        if k==key.RIGHT: a[0] = +1.0
        if k==key.UP:    a[1] = +1.0
        if k==key.DOWN:  a[2] = +0.8   # set 1.0 for wheels to block to zero rotation
        if k==ord('e'):
            app.policy_index = -1
        elif k >= ord('1') and k <= ord('9'):
            app.policy_index = min(len(allac)-1, int(k - ord('1')))
        elif k==ord('a'):
            app.print_action ^= True
        elif k==ord('s'):
            FLAGS.sample_action = 0. if FLAGS.sample_action else 1.
        elif k==ord('i'):
            app.show_state_image = True
        elif k==ord('t'):
            training.enable ^= True
        elif k==ord('k'):
            # Bootstrap learning with user-supplied trajectories, then turn them off
            FLAGS.seq_keep = 0
        elif k==ord('r'):
            state.next_reset = True
        elif k==ord('m'):
            app.draw_attention ^= True
        elif k==ord('w'):
            app.wireframe ^= True
        elif k==ord('p'):
            app.pause ^= True
        elif k==ord('q'): on_close()
        else: return
        settings_caption()

    def key_release(k, mod):
        if k==key.LEFT  and a[0]==-1.0: a[0] = 0
        if k==key.RIGHT and a[0]==+1.0: a[0] = 0
        if k==key.UP:    a[1] = 0
        if k==key.DOWN:  a[2] = 0

    envu.isRender = True
    if not hasattr(envu, 'viewer'): # pybullet-gym
        return a
    global window
    env.reset(); env_render() # Needed for viewer.window
    window = envu.viewer.window
    window.on_key_press = key_press
    window.on_key_release = key_release
    window.on_close = on_close
    settings_caption()
    hook_swapbuffers()
    return a

action = Struct(to_take=None, policy=[], keyboard=setup_key_actions())
def step_to_frames():
    def choose_action(value): # Choose from Q-values or softmax policy
        return np.random.choice(value.shape[0], p=softmax(value)) if FLAGS.sample_action else np.argmax(value)
    def interp(f, a, b): return a + f*(b-a)

    a = action.keyboard[:ACTION_DIMS].copy()
    if ACTION_DISCRETE: a = onehot_vector(int(a[0]+1.), ACTION_DIMS)

    if state.count > 0 and app.policy_index != -1:
        a = app.per_inst.policy_sample if FLAGS.sample_action else app.per_inst.policy_mode
        c = app.per_inst.choice_softmax
        #a = a[c.argmax()]
        a = (a * np.expand_dims(c, -1)).sum(0) # Interpolate between options

        if POLICY_SOFTMAX: a = onehot_vector(np.argmax(a), ACTION_DIMS)
        elif POLICY_BETA: a = a * (np.array(ACTION_CLIP[1])-np.array(ACTION_CLIP[0])) + ACTION_CLIP[0]
        else: a = np.clip(a, *ACTION_CLIP)

    '''
    if FLAGS.sample_action:
        np.random.seed(0)
        offset = np.array([FLAGS.sample_action*math.sin(2*math.pi*(r + state.count/20.)) for r in np.random.rand(ACTION_DIMS)])
        a = np.clip(a+offset, -1, 1.)
    '''

    env_action = a
    if ACTION_DISCRETE:
        env_action = np.argmax(a)
        a = onehot_vector(env_action, ACTION_DIMS)
    action_to_save = a

    obs = state.last_obs
    reward_sum = 0.
    state.frames[:-1] = state.frames[1:]
    for frame in range(er.ACTION_REPEAT):
        state.done |= state.next_reset
        state.last_reset += 1
        if state.done:
            state.last_pos_reward = 0
            state.next_reset = False
            state.last_reset = 0
            # New episode
            if FLAGS.env_seed:
                env.seed(int(FLAGS.env_seed))
            obs = env.reset()
        env_render()
        #imshow([obs, test_lcn(obs, sess)[0]])
        state.frames[-1, frame] = obs

        obs, reward, state.done, info = env.step(env_action)
        state.last_pos_reward = 0 if reward>0. else state.last_pos_reward+1
        if ENV_NAME == 'MountainCar-v0':
            # Mountain car env doesnt give any +reward
            reward = 1. if state.done else 0.
        elif ENV_NAME == 'CarRacing-v0' and not FLAGS.record:
            if state.last_pos_reward > 100 or not any([len(w.tiles) for w in envu.car.wheels]):
                state.done = True # Reset track if on grass
                reward = -100
        reward_sum += reward
    state.last_obs = obs
    return [reward_sum], action_to_save

def append_to_batch():
    save_paths = er.seq_paths(training.append_batch)
    if not FLAGS.inst and FLAGS.recreate_states:
        if not state.count:
            training.saved_batch = ermem.mmap_seq(save_paths, 'r', states=False)
        batch = training.saved_batch
        state.frames[-1] = batch.rawframes[state.count]
        save_reward, save_action = batch.rewards[state.count], batch.actions[state.count]
    else:
        save_reward, save_action = step_to_frames()

    r, app.per_inst = ops_run('per_inst', {ph.frame: state.frames})
    if app.print_action:
        ops_print(app.per_inst)
    save_state = r[0]
    if app.print_action:
        print_section(dict(header='print_action',
            #save_reward=save_reward,
            save_action=save_action))
        os.system('clear')

    if app.show_state_image:
        app.show_state_image = False
        proc = multiprocessing.Process(target=imshow,
            args=([save_state[0,:,:,CHANNELS*i:CHANNELS*(i+1)] for i in range(er.ACTION_REPEAT)],))
        proc.start()

    temp_paths = er.seq_paths(FLAGS.inst, 'temp')
    if not training.temp_batch:
        training.temp_batch = ermem.mmap_seq(temp_paths, 'w+')
    batch = training.temp_batch
    batch.rawframes[state.count] = state.frames[-1]
    batch.states[state.count] = save_state[-1]
    batch.rewards[state.count] = save_reward
    batch.actions[state.count] = save_action

    state.count += 1
    if state.count == er.TRAJECTORY_LENGTH:
        if FLAGS.inst or training.seq_recorded < FLAGS.seq_keep:
            print('Replacing batch #%i' % training.append_batch)
            for a in batch.arrays: del a
            training.temp_batch = None

            # Rename inst batch files into server's ER batches.
            for k in save_paths.keys():
                src = temp_paths[k]
                dst = save_paths[k]
                os.system('rm -f ' + dst)
                os.system('mv ' + src + ' ' + dst)

        training.seq_recorded += 1
        training.append_batch += 1
        if FLAGS.inst and training.seq_recorded == FLAGS.seq_per_inst:
            training.seq_recorded = 0
            training.append_batch = FIRST_SEQ
        state.count = 0

def print_section(d):
    np.set_printoptions(suppress=True, precision=6, sign=' ')
    print('====' + d['header'] + '====')
    for k in sorted(d.keys()):
        v = str(d[k])
        if k == 'header' or len(v.split('\n')) > 10: # Skip large arrays
            continue
        if v.find('\n') != -1:
            print(str(k) + ': ')
            print(v) # Print on newline for multidimensional array alignment
        else:
            print(k + ': ' + v)
    print('')

def ops_finish(sname):
    named_ops = ops.__dict__[sname]
    keys = sorted(allac[0].__dict__[sname].__dict__.keys())
    named_ops += [tf.concat([[allac[r].__dict__[sname].__dict__[s]]
        for r in range(len(allac))], 0) for s in keys]

def ops_run(sname, feed_dict):
    named_ops = ops.__dict__[sname]
    post_ops = ops.__dict__[sname.replace('per_', 'post_')]

    r = sess.run(named_ops, feed_dict)
    sess.run(post_ops)

    policy_index = 0 if app.policy_index==-1 else app.policy_index
    keys = sorted(allac[0].__dict__[sname].__dict__.keys())
    d = {s: r[-i-1][policy_index] for i,s in enumerate(reversed(keys))}
    return r, Struct(header=sname, **d)
def ops_print(output_struct):
    print_section(output_struct.__dict__)

if not FLAGS.inst:
    init_vars()
    ops.per_mb += tf.get_collection(tf.GraphKeys.UPDATE_OPS) # batch_norm
    if FLAGS.summary:
        train_writer = tf.summary.FileWriter(FLAGS.summary, sess.graph)
        merged = tf.summary.merge_all()
        ops.per_mb.insert(0, merged)

    ops_finish('per_mb')
    ops_finish('per_update')
ops_finish('per_inst')

def train_minibatch(mb):
    start_time = time.time()
    # Upload & train minibatch
    feed_dict = {
        ph.states: mb.states[0],
        ph.actions: mb.actions,
        ph.rewards: mb.rewards[:,0],
        ph.mb_count: app.mb_count}
    mb.feed_dict = feed_dict
    r, per_mb = ops_run('per_mb', feed_dict)
    per_mb.elapsed = time.time() - start_time
    ops_print(per_mb)
    # Prioritized experience replay according to TD error.
    mb.priority[:] = per_mb.priority

    app.mb_count += 1
    print_section(dict(
        header='train_minibatch',
        nsteps=FLAGS.nsteps,
        minibatch=app.mb_count,
        rates=dict(learning_rate=FLAGS.learning_rate, gamma=FLAGS.gamma),
        batches=dict(keep=FLAGS.seq_keep,inst=FLAGS.seq_inst,minibatch=FLAGS.minibatch)))
    os.system('clear') # Scroll up to see status

    if FLAGS.summary and not app.mb_count%100:
        summary = r[0]
        train_writer.add_summary(summary, app.mb_count)

def train_update_policy():
    if app.mb_count >= FLAGS.update_mb:
        if not app.mb_count % FLAGS.update_mb:
            _, train_update_policy.output = ops_run('per_update', {})
        #ops_print(train_update_policy.output)

ermem = er.ERMemory([1], STATE_DIM, ACTION_DIMS, FRAME_DIM)
def rl_loop():
    if app.quit: return False
    if FLAGS.inst or not training.enable or training.seq_recorded < FLAGS.seq_keep:
        append_to_batch()
    else:
        if app.pause:
            time.sleep(0.1)
        else:
            global mb
            mb = ermem.fill_mb()
            if mb != None:
                if not app.mb_count % er.TRAJECTORY_LENGTH:
                    sess.run(ops.new_batches)
                train_minibatch(mb)
                train_update_policy()
        env_render() # Render needed for keyboard events
    return True
import utils; utils.loop_while(rl_loop)
