import os,time, json,datetime

# CUDA config
# os.environ["CUDA_VISIBLE_DEVICES"] = "1"
mem_limit=1.0

import tensorflow as tf
import numpy as np
import helpers.loader as loader
import helpers.preprocessing as preprocessing
import helpers.online_moments as online_moments
from helpers.ops import byte_token_array_to_str
from helpers.output import output_pretty, output_basic, tokens_to_string, output_eval
from tqdm import tqdm

from seq2seq_model import Seq2SeqModel
from maluuba_model import MaluubaModel

from datasources.squad_streamer import SquadStreamer

import flags
FLAGS = tf.app.flags.FLAGS

import helpers.metrics as metrics


# TODO: Move this somewhere more appropriate
# unpack a batch, duplicate the components, and insert a pred into the first half
# schema is (c,q,a) and (raw,ids,len,?ans_pos)
def duplicate_batch_and_inject(batch, pred_q_ids, pred_q_str, pred_q_lens):
    new_batch=[]
    for i,x in enumerate(batch):
        new_subbatch=[]
        for j,y in enumerate(x):
            if i==1 and j==0:
                # create a valid padded batch
                new_str_batch=pred_q_str.tolist()+y.tolist()
                max_len = max([len(q) for q in new_str_batch])
                new_str_batch = [q+[loader.PAD.encode() for k in range(max_len-len(q))] for q in new_str_batch]
                new_subbatch.append(np.asarray(new_str_batch))
            elif i==1 and j==1:
                # create a valid padded batch
                new_id_batch=pred_q_ids.tolist()+y.tolist()
                max_len = max([len(q) for q in new_id_batch])
                new_id_batch = [q+[0 for k in range(max_len-len(q))] for q in new_id_batch]
                new_subbatch.append(np.asarray(new_id_batch))
            elif i==1 and j==2:
                new_subbatch.append(np.asarray(pred_q_lens.tolist()+y.tolist()))
            else:
                new_subbatch.append(np.asarray(y.tolist()+y.tolist())) # just duplicate
        new_batch.append(tuple(new_subbatch))
    return tuple(new_batch)



def main(_):
    if FLAGS.testing:
        print('TEST MODE - reducing model size')
        FLAGS.context_encoder_units =100
        FLAGS.answer_encoder_units=100
        FLAGS.decoder_units=100
        FLAGS.batch_size =8
        FLAGS.eval_batch_size=8
        # FLAGS.embedding_size=50

    run_id = str(int(time.time()))
    chkpt_path = FLAGS.model_dir+'qgen/'+FLAGS.model_type+'/'+run_id
    # restore_path=FLAGS.model_dir+'qgen/'+'MALUUBA_FILT'+'/'+'1529573713'
    restore_path=FLAGS.model_dir+'saved/qgen-maluuba-filt'

    if not os.path.exists(chkpt_path):
        os.makedirs(chkpt_path)

    # load dataset
    train_data = loader.load_squad_triples(FLAGS.data_path, False)
    dev_data = loader.load_squad_triples(FLAGS.data_path, True)

    if FLAGS.filter_window_size >-1:
        train_data = preprocessing.filter_squad(train_data, window_size=FLAGS.filter_window_size, max_tokens=FLAGS.filter_max_tokens)
        dev_data = preprocessing.filter_squad(dev_data, window_size=FLAGS.filter_window_size, max_tokens=FLAGS.filter_max_tokens)

    if FLAGS.testing:
        train_data=train_data[:1000]
        num_dev_samples=100
    else:
        num_dev_samples=1000

    print('Loaded SQuAD with ',len(train_data),' triples')
    train_contexts, train_qs, train_as,train_a_pos = zip(*train_data)

    if FLAGS.restore:
        with open(restore_path+'/vocab.json') as f:
            vocab = json.load(f)
    else:
        vocab = loader.get_vocab(train_contexts, tf.app.flags.FLAGS.vocab_size)
        with open(chkpt_path+'/vocab.json', 'w') as outfile:
            json.dump(vocab, outfile)



    # Create model
    if FLAGS.model_type[:7] == "SEQ2SEQ":
        model = Seq2SeqModel(vocab, training_mode=True)
    elif FLAGS.model_type[:7] == "MALUUBA":
        # TEMP
        if not FLAGS.policy_gradient:
            FLAGS.qa_weight = 0
            FLAGS.lm_weight = 0
        model = MaluubaModel(vocab, training_mode=True, lm_weight=FLAGS.lm_weight, qa_weight=FLAGS.qa_weight)
        # if FLAGS.model_type[:10] == "MALUUBA_RL":
        #     qa_vocab=model.qa.vocab
        #     lm_vocab=model.lm.vocab
    else:
        exit("Unrecognised model type: "+FLAGS.model_type)

    # create data streamer
    train_data_source = SquadStreamer(vocab, FLAGS.batch_size, FLAGS.num_epochs, shuffle=True)
    dev_data_source = SquadStreamer(vocab, FLAGS.eval_batch_size, 1, shuffle=True)

    with model.graph.as_default():
        saver = tf.train.Saver()


    # change visible devices if using RL models
    gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=mem_limit, visible_device_list='0',allow_growth = True)
    with tf.Session(config=tf.ConfigProto(gpu_options=gpu_options, allow_soft_placement=False), graph=model.graph) as sess:

        summary_writer = tf.summary.FileWriter(FLAGS.log_dir+'qgen/'+FLAGS.model_type+'/'+run_id, sess.graph)

        train_data_source.initialise(train_data)

        num_steps_train = len(train_data)//FLAGS.batch_size
        num_steps_dev = num_dev_samples//FLAGS.batch_size

        if FLAGS.restore:
            saver.restore(sess, restore_path+ '/model.checkpoint')
            start_e=15#FLAGS.num_epochs
            print('Loaded model')
        else:
            start_e=0
            sess.run(tf.global_variables_initializer())
            # sess.run(model.glove_init_ops)

            f1summary = tf.Summary(value=[tf.Summary.Value(tag="dev_perf/f1",
                                             simple_value=0.0)])
            bleusummary = tf.Summary(value=[tf.Summary.Value(tag="dev_perf/bleu",
                                      simple_value=0.0)])

            summary_writer.add_summary(f1summary, global_step=start_e*num_steps_train)
            summary_writer.add_summary(bleusummary, global_step=start_e*num_steps_train)


        # Initialise the dataset
        # sess.run(model.iterator.initializer, feed_dict={model.context_ph: train_contexts,
        #                                   model.qs_ph: train_qs, model.as_ph: train_as, model.a_pos_ph: train_a_pos})



        best_oos_nll=1e6

        lm_score_moments = online_moments.OnlineMoment()
        qa_score_moments = online_moments.OnlineMoment()

        for e in range(start_e,start_e+FLAGS.num_epochs):
            # Train for one epoch
            for i in tqdm(range(num_steps_train), desc='Epoch '+str(e)):
                # Get a batch
                train_batch, curr_batch_size = train_data_source.get_batch()

                # Are we doing policy gradient? Do a forward pass first, then build the PG batch and do an update step
                if FLAGS.model_type[:10] == "MALUUBA_RL" and FLAGS.policy_gradient:

                    # do a fwd pass first, get the score, then do another pass and optimize
                    qhat_str,qhat_ids, qhat_lens= sess.run([model.q_hat_beam_string, model.q_hat_beam_ids, model.q_hat_beam_lens],
                        feed_dict={model.input_batch: train_batch,
                        model.is_training:False,
                        model.hide_answer_in_copy: True})

                    # Get reward values
                    lm_score = (-1*model.lm.get_seq_perplexity(byte_token_array_to_str(qhat_str, qhat_lens))).tolist() # lower perplexity is better

                    # The output is as long as the max allowed len - remove the pointless extra padding
                    qhat_ids = qhat_ids[:,:np.max(qhat_lens)]
                    qhat_str = qhat_str[:,:np.max(qhat_lens)]

                    qa_pred = model.qa.get_ans(byte_token_array_to_str(train_batch[0][0], train_batch[0][3]), byte_token_array_to_str(qhat_str, qhat_lens))
                    qa_pred_gold = model.qa.get_ans(byte_token_array_to_str(train_batch[0][0], train_batch[0][3]), byte_token_array_to_str(train_batch[1][0], train_batch[1][2]))

                    gold_str=[]
                    # pred_str=[]
                    qa_f1s = []

                    gold_str = byte_token_array_to_str(train_batch[2][0], train_batch[2][2], is_array=False)
                    # pred_str = byte_token_array_to_str([train_batch[0][0][b][qa_pred[b][0]:qa_pred[b][1]] for b in range(curr_batch_size)], is_array=False)

                    qa_f1s.extend([metrics.f1(gold_str[b], qa_pred[b]) for b in range(curr_batch_size)])

                    lm_score_moments.push(lm_score)
                    qa_score_moments.push(qa_f1s)

                    # A variant of popart
                    qa_score_whitened = (qa_f1s-qa_score_moments.mean)/np.sqrt(qa_score_moments.variance+1e-6)
                    lm_score_whitened = (lm_score-lm_score_moments.mean)/np.sqrt(lm_score_moments.variance+1e-6)

                    lm_summary = tf.Summary(value=[tf.Summary.Value(tag="rl_rewards/lm",
                                                     simple_value=np.mean(lm_score))])
                    summary_writer.add_summary(lm_summary, global_step=(e*num_steps_train+i))
                    qa_summary = tf.Summary(value=[tf.Summary.Value(tag="rl_rewards/qa",
                                                     simple_value=np.mean(qa_f1s))])
                    summary_writer.add_summary(qa_summary, global_step=(e*num_steps_train+i))
                    lm_white_summary = tf.Summary(value=[tf.Summary.Value(tag="rl_rewards/lm_white",
                                                     simple_value=np.mean(lm_score_whitened))])
                    summary_writer.add_summary(lm_white_summary, global_step=(e*num_steps_train+i))
                    qa_white_summary = tf.Summary(value=[tf.Summary.Value(tag="rl_rewards/qa_white",
                                                     simple_value=np.mean(qa_score_whitened))])
                    summary_writer.add_summary(qa_white_summary, global_step=(e*num_steps_train+i))

                    # Build a combined batch - half ground truth for MLE, half generated for PG
                    train_batch_ext = duplicate_batch_and_inject(train_batch, qhat_ids, qhat_str, qhat_lens)

                    # print(qhat_ids)
                    # print(qhat_lens)
                    # print(train_batch_ext[2][2])

                    rl_dict={model.lm_score: np.asarray((lm_score_whitened*FLAGS.lm_weight).tolist()+[1 for b in range(curr_batch_size)]),
                        model.qa_score: np.asarray((qa_score_whitened*FLAGS.qa_weight).tolist()+[0 for b in range(curr_batch_size)]),
                        model.rl_lm_enabled: True,
                        model.rl_qa_enabled: True,
                        model.hide_answer_in_copy: True}

                    # perform a policy gradient step, but combine with a XE step by using appropriate rewards
                    ops = [model.pg_optimizer, model.train_summary,model.q_hat_string]
                    if i%FLAGS.eval_freq==0:
                        ops.extend([ model.q_hat_ids, model.question_ids, model.copy_prob, model.q_gold])
                        res_offset = 4
                    else:
                        res_offset=0
                    ops.extend([model.lm_loss, model.qa_loss])
                    res= sess.run(ops, feed_dict={model.input_batch: train_batch_ext,
                        model.is_training:False,
                        **rl_dict})
                    summary_writer.add_summary(res[1], global_step=(e*num_steps_train+i))

                    # Log only the first half of the PG related losses
                    lm_loss_summary = tf.Summary(value=[tf.Summary.Value(tag="train_loss/lm",
                                                     simple_value=np.mean(res[3+res_offset][:curr_batch_size]))])
                    summary_writer.add_summary(lm_loss_summary, global_step=(e*num_steps_train+i))
                    qa_loss_summary = tf.Summary(value=[tf.Summary.Value(tag="train_loss/qa",
                                                     simple_value=np.mean(res[4+res_offset][:curr_batch_size]))])
                    summary_writer.add_summary(qa_loss_summary, global_step=(e*num_steps_train+i))

                else:
                    # Normal single pass update step. If model has PG capability, fill in the placeholders with empty values
                    if FLAGS.model_type[:7] == "MALUUBA" and not FLAGS.policy_gradient:
                        rl_dict={model.lm_score: [0 for b in range(curr_batch_size)],
                            model.qa_score: [0 for b in range(curr_batch_size)],
                            model.rl_lm_enabled: False,
                            model.rl_qa_enabled: False,
                            model.hide_answer_in_copy: False}
                    else:
                        rl_dict={}

                    # Perform a normal optimizer step
                    ops = [model.optimizer, model.train_summary,model.q_hat_string]
                    if i%FLAGS.eval_freq==0:
                        ops.extend([ model.q_hat_ids, model.question_ids, model.copy_prob, model.question_raw])
                    res= sess.run(ops, feed_dict={model.input_batch: train_batch,
                        model.is_training:True,
                        **rl_dict})
                    summary_writer.add_summary(res[1], global_step=(e*num_steps_train+i))



                # Dump some output periodically
                if i%FLAGS.eval_freq==0:
                    with open(FLAGS.log_dir+'out.htm', 'w') as fp:
                        fp.write(output_pretty(res[2].tolist(), res[3], res[4], res[5], e, i))

                    f1s=[]
                    bleus=[]
                    for b, pred in enumerate(res[2]):
                        pred_str = tokens_to_string(pred)
                        gold_str = tokens_to_string(res[6][b])
                        f1s.append(metrics.f1(gold_str, pred_str))
                        bleus.append(metrics.bleu(gold_str, pred_str))


                    f1summary = tf.Summary(value=[tf.Summary.Value(tag="train_perf/f1",
                                                     simple_value=sum(f1s)/len(f1s))])
                    bleusummary = tf.Summary(value=[tf.Summary.Value(tag="train_perf/bleu",
                                              simple_value=sum(bleus)/len(bleus))])

                    summary_writer.add_summary(f1summary, global_step=(e*num_steps_train+i))
                    summary_writer.add_summary(bleusummary, global_step=(e*num_steps_train+i))

            # Evaluate against dev set
            f1s=[]
            bleus=[]
            nlls=[]

            np.random.shuffle(dev_data)
            dev_subset = dev_data[:num_dev_samples]
            dev_data_source.initialise(dev_subset)
            for i in tqdm(range(num_steps_dev), desc='Eval '+str(e)):
                dev_batch, curr_batch_size = dev_data_source.get_batch()
                pred_batch,pred_ids,pred_lens,gold_batch, gold_lens,ctxt,ctxt_len,ans,ans_len,nll= sess.run([model.q_hat_beam_string, model.q_hat_beam_ids,model.q_hat_beam_lens,model.question_raw, model.question_length, model.context_raw, model.context_length, model.answer_locs, model.answer_length, model.nll], feed_dict={model.input_batch: dev_batch ,model.is_training:False})

                nlls.extend(nll.tolist())
                # out_str="<h1>"+str(e)+' - '+str(datetime.datetime.now())+'</h1>'
                for b, pred in enumerate(pred_batch):
                    pred_str = tokens_to_string(pred[:pred_lens[b]-1])
                    gold_str = tokens_to_string(gold_batch[b][:gold_lens[b]-1])
                    f1s.append(metrics.f1(gold_str, pred_str))
                    bleus.append(metrics.bleu(gold_str, pred_str))
                    # out_str+=pred_str.replace('>','&gt;').replace('<','&lt;')+"<br/>"+gold_str.replace('>','&gt;').replace('<','&lt;')+"<hr/>"
                if i==0:
                    title=chkpt_path
                    out_str = output_eval(title,pred_batch,  pred_ids, pred_lens, gold_batch, gold_lens, ctxt, ctxt_len, ans, ans_len)
                    with open(FLAGS.log_dir+'out_eval_'+FLAGS.model_type+'.htm', 'w') as fp:
                        fp.write(out_str)

            f1summary = tf.Summary(value=[tf.Summary.Value(tag="dev_perf/f1",
                                             simple_value=sum(f1s)/len(f1s))])
            bleusummary = tf.Summary(value=[tf.Summary.Value(tag="dev_perf/bleu",
                                      simple_value=sum(bleus)/len(bleus))])
            nllsummary = tf.Summary(value=[tf.Summary.Value(tag="dev_perf/nll",
                               simple_value=sum(nlls)/len(nlls))])

            summary_writer.add_summary(f1summary, global_step=((e+1)*num_steps_train))
            summary_writer.add_summary(bleusummary, global_step=((e+1)*num_steps_train))
            summary_writer.add_summary(nllsummary, global_step=((e+1)*num_steps_train))

            mean_nll=sum(nlls)/len(nlls)
            if mean_nll < best_oos_nll:
                print("New best NLL! ", mean_nll, " Saving...")
                best_oos_nll = mean_nll
                saver.save(sess, chkpt_path+'/model.checkpoint')
            else:
                print("NLL not improved ", mean_nll)
                if FLAGS.model_type[:10] == "MALUUBA_RL":
                    print("Saving anyway")
                    saver.save(sess, chkpt_path+'/model.checkpoint')
if __name__ == '__main__':
    tf.app.run()
