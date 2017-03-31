import os
import re
import sys
import glob
import pandas
from collections import defaultdict

import numpy as np
from tensorflow.python.platform import gfile
import tensorflow as tf
import cPickle as pickle

tf.app.flags.DEFINE_string("data_dir", "/s0/ttmt001/speech_parsing", \
        "directory of swbd data files")
tf.app.flags.DEFINE_string("output_dir", "/s0/ttmt001/speech_parsing/prosody", \
        "directory of output files")
tf.app.flags.DEFINE_integer("sp_scale", 10, "scaling of input buckets")
tf.app.flags.DEFINE_integer("avg_frame", 5, "number of frames to average over")
FLAGS = tf.app.flags.FLAGS

data_dir = FLAGS.data_dir
output_dir = FLAGS.output_dir

mfcc_dir = data_dir + '/swbd_mfcc'
time_dir = data_dir + '/swbd_trees'
pitch_dir = data_dir + '/swbd_pitch'
pitch_pov_dir = data_dir + '/swbd_pitch_pov'
fbank_dir = data_dir + '/swbd_fbank'
pause_dir = data_dir + '/pause_data'

hop = 10.0 # in msec
num_sec = 0.04  # amount of time to approximate extra frames when no time info available

# Regular expressions used to tokenize.
_WORD_SPLIT = re.compile(b"([.,!?\"':;)(])")
_DIGIT_RE = re.compile(br"\d")

# Special vocabulary symbols - we always put them at the start.
_PAD = b"_PAD"
_GO = b"_GO"
_EOS = b"_EOS"
_UNK = b"_UNK"
_UNF = b"_UNF"
_START_VOCAB = [_PAD, _GO, _EOS, _UNK, _UNF]
_PUNCT = ["'", "`",'"', ",", ".", "/", "?", "[", "]", "(", ")", "{", "}", ":",
";", "!"]

PAD_ID = 0
GO_ID = 1
EOS_ID = 2
UNK_ID = 3
UNF_ID = 4

# Use the following buckets: 
#_buckets = [(10, 40), (25, 85), (40, 150)]
_buckets = [(10, 40), (25, 100), (50, 200), (100, 350)]

def basic_tokenizer(sentence):
  """Very basic tokenizer: split the sentence into a list of tokens."""
  words = []
  for space_separated_fragment in sentence.strip().split():
    words.extend(re.split(_WORD_SPLIT, space_separated_fragment))
  return [w for w in words if w]

def initialize_vocabulary(vocabulary_path):
    if gfile.Exists(vocabulary_path):
        rev_vocab = []
        with gfile.GFile(vocabulary_path, mode="rb") as f:
            rev_vocab.extend(f.readlines())
        rev_vocab = [line.strip() for line in rev_vocab]
        vocab = dict([(x, y) for (y, x) in enumerate(rev_vocab)])
        return vocab, rev_vocab
    else:
        raise ValueError("Vocabulary file %s not found.", vocabulary_path)

def sentence_to_token_ids(sentence, vocabulary, normalize_digits=True, lower=False):
    words = sentence.strip().split()
    if lower: words = [w.lower() for w in words]
    if not normalize_digits:
        return [vocabulary.get(w, UNK_ID) for w in words]
    # Normalize UNF and digits by 0 before looking words up in the vocabulary.
    for i, w in enumerate(words):
        if w[-1] == '-': words[i] = _UNF  # unfinished word normalization
    return [vocabulary.get(re.sub(_DIGIT_RE, b"0", w), UNK_ID) for w in words]

def summarize_mfcc(mfccs):
    start_indices = np.arange(0, len(mfccs), FLAGS.avg_frame)
    mfccs = np.array(mfccs)
    summarized = [np.mean(mfccs[offset:offset+FLAGS.avg_frame, :], axis=0) \
            for offset in start_indices]
    return summarized    

def make_array(frames):
    return np.array(frames).T

def convert_to_array(str_vector):
    str_vec = str_vector.replace('[','').replace(']','').replace(',','').split()
    num_list = []
    for x in str_vec:
        x = x.strip()
        if x != 'None': num_list.append(float(x))
        else: num_list.append(np.nan)
    return num_list

def has_bad_alignment(num_list):
    for i in num_list:
        if i < 0 or np.isnan(i): return True
    return False

def find_bad_alignment(num_list):
    bad_align = []
    for i in range(len(num_list)):
        if num_list[i] < 0 or np.isnan(num_list[i]): 
            bad_align.append(i)
    return bad_align

def has_bad_edge(num_list):
    start = num_list[0]
    end = num_list[-1]   
    if start<-1 or end<-1: return True
    if np.isnan(start) or np.isnan(end): return True
    return False

def prune_punct(num_list, tokens):
    new_list = []
    assert len(num_list)==len(tokens)
    for i in range(len(num_list)):
        if tokens[i] in _PUNCT: continue
        new_list.append(num_list[i])
    return new_list

def check_valid(num):
    if num < 0 or np.isnan(num): return False
    return True

def clean_up(stimes, etimes):
    if not check_valid(stimes[-1]):
        stimes[-1] = max(etimes[-1] - num_sec, 0)
    
    if not check_valid(etimes[0]):
        etimes[0] = stimes[0] + num_sec
    
    for i in range(1,len(stimes)-1):
        this_st = stimes[i]
        prev_st = stimes[i-1]
        next_st = stimes[i+1]

        this_et = etimes[i]
        prev_et = etimes[i-1]
        next_et = etimes[i+1]   
   
        if not check_valid(this_st) and check_valid(prev_et):
            stimes[i] = prev_et

        if not check_valid(this_st) and check_valid(prev_st):
            stimes[i] = prev_st + num_sec

    for i in range(1,len(etimes)-1)[::-1]:
        this_st = stimes[i]
        prev_st = stimes[i-1]
        next_st = stimes[i+1]

        this_et = etimes[i]
        prev_et = etimes[i-1]
        next_et = etimes[i+1]   
        if not check_valid(this_et) and check_valid(next_st):
            etimes[i] = next_st

        if not check_valid(this_et) and check_valid(next_et):
            etimes[i] = next_et - num_sec

    return stimes, etimes

def get_stats(split):
    lengths = []
    wlens = []
    data_file = os.path.join(time_dir, split + '.data.csv')
    df = pandas.read_csv(data_file, sep='\t')
    sw_files = set(df.file_id.values)
    for sw in sw_files:
        this_dict = defaultdict(dict) 
        for speaker in ['A', 'B']:
            mfcc_file = os.path.join(mfcc_dir, sw + '-' + speaker + '.pickle')
            pitch_file = os.path.join(pitch_dir, sw + '-' + speaker + '.pickle')
            pitch_pov_file = os.path.join(pitch_pov_dir,sw+'-'+speaker+'.pickle')
            fbank_file = os.path.join(fbank_dir, sw + '-' +speaker+'.pickle')
            try:
                data_pitch = pickle.load(open(pitch_file))
            except: 
                print("No pitch file for ", sw, speaker)
                continue
            pitchs = data_pitch.values()[0]
            this_df = df[(df.file_id==sw)&(df.speaker==speaker)]
            for i, row in this_df.iterrows():
                tokens = row.sentence.strip().split()
                stimes = convert_to_array(row.start_times)
                etimes = convert_to_array(row.end_times)
                
                if len(stimes)==1: 
                    if (not check_valid(stimes[0])) or (not check_valid(etimes[0])):
                        continue
                 
                # go back 5 frames approximately
                if check_valid(stimes[0]): 
                    begin = stimes[0]
                else:
                    if check_valid(etimes[0]): 
                        begin = max(etimes[0] - num_sec, 0) 
                        stimes[0] = begin
                    elif check_valid(stimes[1]):
                        begin = max(stimes[1] - num_sec, 0)
                        stimes[0] = begin
                    else:
                        continue

                if check_valid(etimes[-1]): 
                    end = etimes[-1]
                else:
                    if check_valid(stimes[-1]): 
                        end = stimes[-1] + num_sec
                        etimes[-1] = end
                    elif check_valid(etimes[-2]):
                        end = etimes[-2] + num_sec
                        etimes[-1] = end
                    else:
                        continue
                
                s_ms = begin*1000 # in msec
                e_ms = end*1000

                s_frame = int(np.floor(s_ms / hop))
                e_frame = int(np.ceil(e_ms / hop))
                pf_frames = pitchs[s_frame:e_frame] 

                # final clean up
                stimes, etimes = clean_up(stimes, etimes)
                assert len(stimes) == len(etimes) == len(tokens)

                sframes = [int(np.floor(x*100)) for x in stimes]
                eframes = [int(np.ceil(x*100)) for x in etimes]
                word_lengths = [e-s for s,e in zip(sframes,eframes)]
                invalid = [x for x in word_lengths if x <=0]
                if len(invalid)>0: 
                    print begin, stimes[0], etimes[0] 
                    print 
                    continue
                #print wlens
                wlens += word_lengths

                # TESTS PASSED
                if find_bad_alignment(stimes): 
                    six = find_bad_alignment(stimes)
                    print [tokens[i] for i in six], stimes, etimes 
                if find_bad_alignment(etimes): 
                    eix = find_bad_alignment(etimes)
                    print [tokens[i] for i in eix], stimes, etimes

                seq_len = len(pf_frames)
                lengths.append(seq_len)

    '''
    num5 = len([x for x in lengths if x<5])
    num25 = len([x for x in lengths if x<25])
    num100 = len([x for x in lengths if x<100])
    num1500 = len([x for x in lengths if x>=1500])
    num2000 = len([x for x in lengths if x>=2000])
    num3000 = len([x for x in lengths if x>=3000])
    num3500 = len([x for x in lengths if x>=3500])
    
    lengths = np.array(lengths)
    print "Split    | avg length    | max length    | min length"
    print split, np.mean(lengths), max(lengths), min(lengths)
    print "Number of sentences with < 5/25/100 frames; total #sentences:"
    print num5, num25, num100, len(lengths)
    print "Number of sentences with >= 1500/2000/3000/3500 frames:"
    print num1500, num2000, num3000, num3500
    '''

    #num5 = len([x for x in wlens if x<5])
    #num25 = len([x for x in wlens if x<25])
    #num100 = len([x for x in wlens if x<100])

    num50p = len([x for x in wlens if x>=50])
    num75p = len([x for x in wlens if x>=75])
    #num100p = len([x for x in wlens if x>=100])
    #num200p = len([x for x in wlens if x>=200])
    #num500p = len([x for x in wlens if x>=500])
    
    wlens = np.array(wlens)
    print "word level stats"
    #print "Split    | avg length    | max length    | min length"
    #print split, np.mean(wlens), max(wlens), min(wlens)
    #print "Number of words with < 5/25/100 frames; total #words:"
    #print num5, num25, num100, len(wlens)
    #print "Number of words with >= 100/200/500 frames; total #words:"
    #print num100p, num200p, num500p
    print "Number of words with >= 50/75 frames; total #words:"
    print num50p, num75p


def make_dict(pause_data):
    pauses = dict()
    for bucket in pause_data:
        for sample in bucket:
            sent_id, info_dict, parse = sample
            pauses[sent_id] = info_dict
    return pauses


# The following functions perform data processing from raw in the steps:
# 1. look up timing to get appropriate mfcc and pitch frames
# 2. convert sentences to token ids
# 3. convert parses to token ids
# 4. put everything in a dictionary
def split_frames(split, feat_types):
    data_file = os.path.join(time_dir, split + '.data.csv')
    pause_file = os.path.join(pause_dir, split+'_nopunc.pickle')
    pause_data = pickle.load(open(pause_file))
    pauses = make_dict(pause_data)
    df = pandas.read_csv(data_file, sep='\t')
    sw_files = set(df.file_id.values)
    for sw in sw_files:
        this_dict = defaultdict(dict) 
        for speaker in ['A', 'B']:
            mfcc_file = os.path.join(mfcc_dir, sw + '-' + speaker + '.pickle')
            pitch_file = os.path.join(pitch_dir, sw + '-' + speaker + '.pickle')
            pitch_pov_file = os.path.join(pitch_pov_dir,sw+'-'+speaker+'.pickle')
            fbank_file = os.path.join(fbank_dir, sw + '-' +speaker+'.pickle')

            for feat in feat_types:
                if 'mfcc' in feat_types:
                    try:
                        data_mfcc = pickle.load(open(mfcc_file))
                    except: 
                        print("No mfcc file for ", sw, speaker)
                        continue
                    mfccs = data_mfcc.values()[0]
                if 'pitch2' in feat_types:
                    try:
                        data_pitch = pickle.load(open(pitch_file))
                    except: 
                        print("No pitch file for ", sw, speaker)
                        continue
                    pitchs = data_pitch.values()[0]
                if 'pitch3' in feat_types:
                    try:
                        data_pitch_pov = pickle.load(open(pitch_pov_file))
                    except: 
                        print("No pitch pov file for ", sw, speaker)
                    pitch_povs = data_pitch_pov.values()[0]
                if 'fbank' in feat_types:
                    try:
                        data_fbank = pickle.load(open(fbank_file))
                    except: 
                        print("No fbank file for ", sw, speaker)
                        continue
                    fbanks = data_fbank.values()[0]

            this_df = df[(df.file_id==sw)&(df.speaker==speaker)]
            for i, row in this_df.iterrows():
                tokens = row.sentence.strip().split()
                stimes = convert_to_array(row.start_times)
                etimes = convert_to_array(row.end_times)
                
                if len(stimes)==1: 
                    if (not check_valid(stimes[0])) or (not check_valid(etimes[0])):
                        continue
                 
                # go back 5 frames approximately
                if check_valid(stimes[0]): 
                    begin = stimes[0]
                else:
                    if check_valid(etimes[0]): 
                        begin = max(etimes[0] - num_sec, 0) 
                        stimes[0] = begin
                    elif check_valid(stimes[1]):
                        begin = max(stimes[1] - num_sec, 0)
                        stimes[0] = begin
                    else:
                        continue

                if check_valid(etimes[-1]): 
                    end = etimes[-1]
                else:
                    if check_valid(stimes[-1]): 
                        end = stimes[-1] + num_sec
                        etimes[-1] = end
                    elif check_valid(etimes[-2]):
                        end = etimes[-2] + num_sec
                        etimes[-1] = end
                    else:
                        continue
                
                # final clean up
                stimes, etimes = clean_up(stimes, etimes)
                assert len(stimes) == len(etimes) == len(tokens)

                sframes = [int(np.floor(x*100)) for x in stimes]
                eframes = [int(np.ceil(x*100)) for x in etimes]
                s_frame = sframes[0]
                e_frame = eframes[-1]
                word_lengths = [e-s for s,e in zip(sframes,eframes)]
                invalid = [x for x in word_lengths if x <=0]
                if len(invalid)>0: 
                    print "End time < start time for: ", row.tokens 
                    print invalid
                    continue

                offset = s_frame
                word_bounds = [(x-offset,y-offset) for x,y in zip(sframes, eframes)]
                assert len(word_bounds) == len(tokens)
                globID = row.sent_id.replace('~','_'+speaker+'_')
                if globID not in pauses: 
                    print "No pause info for sentence: ", globID
                    continue
                this_dict[globID]['sents'] = row.sentence
                this_dict[globID]['parse'] = row.parse
                this_dict[globID]['windices'] = word_bounds
                this_dict[globID]['word_dur'] = [etimes[i]-stimes[i] for i in range(len(stimes))]
                this_dict[globID]['pause_bef'] = pauses[globID]['pause_bef']
                this_dict[globID]['pause_aft'] = pauses[globID]['pause_aft']
                for feat in feat_types:
                    if feat=='mfcc':
                        mf_frames = mfccs[s_frame:e_frame]
                        this_dict[globID]['mfccs'] = mf_frames
                    if feat=='pitch2':
                        pf_frames = pitchs[s_frame:e_frame]
                        this_dict[globID]['pitch2'] = pf_frames
                    if feat=='pitch3':
                        pv_frames = pitch_povs[s_frame:e_frame]
                        this_dict[globID]['pitch3'] = pv_frames
                    if feat=='fbank':
                        fb_frames = fbanks[s_frame:e_frame]
                        this_dict[globID]['fbank'] = fb_frames

        dict_name = os.path.join(output_dir, split, sw + '_prosody.pickle')
        pickle.dump(this_dict, open(dict_name, 'w'))


def norm_energy_by_turn(this_data):
    feat_dim = 41
    turnA = np.empty((feat_dim,0)) 
    turnB = np.empty((feat_dim,0))
    for k in this_data.keys():
        fbank = this_data[k]['fbank']
        fbank = np.array(fbank).T
        if 'A' in k:
            turnA = np.hstack([turnA, fbank])
        else:
            turnB = np.hstack([turnB, fbank])
    meanA = np.mean(turnA, 1) 
    stdA = np.std(turnA, 1)
    meanB = np.mean(turnB, 1)
    stdB = np.std(turnB, 1)
    maxA = np.max(turnA, 1)
    maxB = np.max(turnB, 1)
    return meanA, stdA, meanB, stdB, maxA, maxB 

def process_data_both(data_dir, split, sent_vocab, parse_vocab, normalize=False):
    data_set = [[] for _ in _buckets]
    dur_stats_file = os.path.join(data_dir, 'avg_word_stats.pickle')
    dur_stats = pickle.load(open(dur_stats_file))
    global_mean = np.mean([x['mean'] for x in dur_stats.values()])
    split_path = os.path.join(data_dir, split)
    split_files = glob.glob(split_path + "/*")
    for file_path in split_files:
        this_data = pickle.load(open(file_path))

        if normalize:
            meanA, stdA, meanB, stdB, maxA, maxB  = norm_energy_by_turn(this_data)

        for k in this_data.keys():
            sentence = this_data[k]['sents']
            parse = this_data[k]['parse']
            windices = this_data[k]['windices']
            pause_bef = this_data[k]['pause_bef']
            pause_aft = this_data[k]['pause_aft']

            # features needing normalization
            word_dur = this_data[k]['word_dur']
            pitch3 = make_array(this_data[k]['pitch3'])
            fbank = make_array(this_data[k]['fbank'])
            if normalize:
                # normalize energy by z-scoring
                if 'A' in k:
                    mu = meanA
                    sigma = stdA
                else:
                    mu = meanB
                    sigma = stdB

                e_total = np.sum(mu[1:])
                e0 = (fbank[0, :] - mu[0]) / sigma[0]
                elow = np.sum(fbank[1:21,:],0)/e_total
                ehigh = np.sum(fbank[21:,:],0)/e_total
                energy = np.array([e0,elow,ehigh])                

                # normalize word durations by dividing by mean
                words = sentence.split()
                assert len(word_dur) == len(words)
                for i in range(len(words)):
                    if words[i] not in dur_stats:
                        print "No mean dur info for word ", words[i]
                        wmean = global_mean
                    wmean = dur_stats[words[i]]['mean']
                    word_dur[i] = word_dur[i]/wmean
            else:
                energy = fbank[0,:].reshape((1,fbank.shape[1]))

            pitch3_energy = np.vstack([pitch3, energy])

            # convert tokens to ids
            sent_ids = sentence_to_token_ids(sentence, sent_vocab, True, True)
            parse_ids = sentence_to_token_ids(parse, parse_vocab, False, False)
            if split != 'extra':
                parse_ids.append(EOS_ID)
            maybe_buckets = [b for b in xrange(len(_buckets)) 
                if _buckets[b][0] >= len(sent_ids) and _buckets[b][1] >= len(parse_ids)]
            if not maybe_buckets: 
                #print(k, sentence, parse)
                continue
            bucket_id = min(maybe_buckets)
            
            data_set[bucket_id].append([sent_ids, parse_ids, windices, pitch3_energy, \
                    pause_bef, pause_aft, word_dur])

    return data_set

def main(_):
    '''
    print "\nCheck dev"
    get_stats('dev')
    print "\nCheck test"
    get_stats('test')
    print "\nCheck train"
    get_stats('train')
    '''
    
    sent_vocabulary_path = os.path.join(output_dir, 'vocab.sents') 
    parse_vocabulary_path = os.path.join(output_dir, 'vocab.parse')
    parse_vocab, _ = initialize_vocabulary(parse_vocabulary_path)
    sent_vocab, _ = initialize_vocabulary(sent_vocabulary_path)

    split = 'train'
    # split frames into utterances first
    #feats = ['pitch3', 'fbank'] 
    #split_frames(split, feats)  # ==> dumps to output_dir

    # normalize and process data into buckets
    normalize = True
    this_set = process_data_both(output_dir, split, sent_vocab, parse_vocab, normalize)
    this_file = os.path.join(output_dir, split + '_prosody_normed.pickle')
    pickle.dump(this_set, open(this_file,'w'))

if __name__ == "__main__":
    tf.app.run()


