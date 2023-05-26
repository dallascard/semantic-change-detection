import os
import re
import json
from collections import Counter
from optparse import OptionParser

import torch
import numpy as np
import pandas as pd
from tqdm import tqdm
from transformers import BertModel, BertTokenizerFast, BertForMaskedLM
from transformers.models.bert.modeling_bert import BertOnlyMLMHead

from common.stopwords import get_stopwords
from common.misc import get_model_name, get_subdir


# Embed tokens and save substitutes (without saving vectors)


def main():
    usage = "%prog"
    parser = OptionParser(usage=usage)
    parser.add_option('--basedir', type=str, default='/data/dalc/SemEval/2020/task1_semantic_change/',
                      help='Base dir: default=%default')
    parser.add_option('--lang', type=str, default='eng',
                      help='Language [eng|ger|lat|swe]: default=%default')
    parser.add_option('--model', type=str, default='bert-large-uncased',
                      help='Model that was used for tokenization: default=%default')
    parser.add_option('--strip-accents', action="store_true", default=False,
                      help='Strip accents when tokenizing: default=%default')
    parser.add_option('--random-targets', action="store_true", default=False,
                      help='Use random targets rather than semeval targets: default=%default')
    parser.add_option('--max-samples', type=int, default=4000,
                      help='Max samples per target: default=%default')
    parser.add_option('--max-window-size', type=int, default=50,
                      help='Max window radius (in word tokens): default=%default')
    parser.add_option('--batch-size', type=int, default=4000,
                      help='Batch size: default=%default')
    parser.add_option('--device', type=int, default=0,
                      help='GPU to use: default=%default')
    parser.add_option('--top-k', type=int, default=11,
                      help='Top-k terms to keep: default=%default')
    parser.add_option('--seed', type=int, default=42,
                      help='Random seed: default=%default')

    (options, args) = parser.parse_args()

    basedir = options.basedir
    lang = options.lang
    base_model = options.model
    strip_accents = options.strip_accents
    use_random_targets = options.random_targets
    top_k = options.top_k
    max_samples = options.max_samples
    max_window_size = options.max_window_size
    batch_size = options.batch_size    
    device = options.device
    seed = options.seed

    no_pos = False
    if lang == 'eng':
        no_pos = True

    if use_random_targets:
        index_file = 'random_indices_in_tokens.json'
        output_subdir = 'random_subs_masked'
    elif no_pos:
        index_file = 'target_indices_in_tokens_nopos.json'
        output_subdir = 'subs_masked_nopos'
    else:
        index_file = 'target_indices_in_tokens.json'
        output_subdir = 'subs_masked'

    assert lang in {'eng', 'ger', 'swe', 'lat'}

    basedir = os.path.join(basedir, 'semeval2020_ulscd_' + lang)

    model_name = get_model_name(base_model)

    stopwords = get_stopwords(language=lang)

    np.random.seed(seed)

    tokenized_dir = get_subdir(basedir, model_name, strip_accents)

    trained_model_dir = os.path.join(get_subdir(basedir, model_name, strip_accents, prefix='mlm_pretraining'), 'model')

    print("Loading model")
    tokenizer_class = BertTokenizerFast

    # Load pretrained model/tokenizer
    if lang == 'ger':
        tokenizer = tokenizer_class.from_pretrained(base_model)
        if strip_accents:
            tokenizer.backend_tokenizer.normalizer.strip_accents = True
        else:
            tokenizer.backend_tokenizer.normalizer.strip_accents = False
    else:
        tokenizer = tokenizer_class.from_pretrained(base_model)

    outdir = os.path.join(tokenized_dir, output_subdir)
    if not os.path.exists(outdir):
        os.makedirs(outdir)

    with open(os.path.join(outdir, 'config.json'), 'w') as f:
        json.dump(options.__dict__, f, indent=2)

    infile = os.path.join(tokenized_dir, 'all.jsonlist')
    
    print("Loading infile")
    with open(infile) as f:
        lines = f.readlines()
    lines = [json.loads(line) for line in lines]
    len(lines)
    lines_by_id = {line['id']: line for line in lines}

    index_file = os.path.join(tokenized_dir, index_file)
    with open(index_file) as f:
        indices = json.load(f)

    print("Loading model")
    #if model_type == 'bert':
    model_class = BertModel
    tokenizer_class = BertTokenizerFast
    lm_model_class = BertForMaskedLM
    lm_head_class = BertOnlyMLMHead

    # Load pretrained model/tokenizer
    tokenizer = tokenizer_class.from_pretrained(trained_model_dir)
    model = model_class.from_pretrained(trained_model_dir)

    # move the model to the GPU
    torch.cuda.set_device(device)
    device = torch.device("cuda", device)
    model.to(device)

    lm = lm_model_class.from_pretrained(trained_model_dir)

    # move the model to the GPU
    lm.to(device)

    mlm = None
    for m in lm.modules():
        if type(m) == lm_head_class:
            mlm = m

    mlm.to(device)

    vocab = tokenizer.vocab
    mask_token = tokenizer.mask_token
    vocab_index = {index: term for term, index in vocab.items()}
    sorted_vocab = [vocab_index[i] for i in range(len(vocab))]

    for target_term in sorted(indices):
        pairs = indices[target_term]
        print(target_term, len(pairs))
        target_counter = Counter()
        sub_counter = Counter()
        n_pairs = len(pairs)
        if n_pairs > max_samples:
            subset_indices = np.random.choice(np.arange(len(pairs)), max_samples, replace=False)
            pairs = [pairs[i] for i in subset_indices]
            n_pairs = len(pairs)

        if len(pairs) <= 1:
            print("Skipping", target_term, "with only", n_pairs, "examples")
        
        else:
            # accumulate over all 
            line_ids_to_save = []
            token_indices_to_save = []
            top_words = []
            top_word_probs = []
                
            running_total = 0
            batch = 0

            # accumulate in batch
            instances_in_batch = []
            target_tokens = []
            to_encode = []
            target_word_piece_indices = []

            for index_sample_i, pair in tqdm(enumerate(pairs)):

                line_id = pair[0]
                token_index = pair[1]

                instances_in_batch.append(index_sample_i)

                # get document corresponding to index sample
                line = lines_by_id[line_id]
                
                tokens = line['tokens']
                token = tokens[token_index]
                target_counter[re.sub('##', '', token)] += 1

                # get the full context for now, and put them into strings, joined by spaces
                if token_index > 0:
                    left = ' '.join(tokens[:token_index])
                else:
                    left = ''
                if token_index < len(tokens)-1:
                    right = ' '.join(tokens[token_index+1:])
                else:
                    right = ''
                
                # split both into word pieces
                left = re.sub('##', ' ##', left)
                right = re.sub('##', ' ##', right)
                left = left.split()
                right = right.split()
                
                # restrict the window size
                left = left[-max_window_size:]
                right = right[:max_window_size]
                pieces_to_encode = left + [mask_token] + right 

                word_pieces_index = len(left) 

                # keep track of a few things for debugging
                target_tokens.append(token)
                to_encode.append(pieces_to_encode)
                target_word_piece_indices.append(word_pieces_index)
                
                line_ids_to_save.append(line_id)
                token_indices_to_save.append(token_index)

                if len(to_encode) == batch_size or (index_sample_i == (len(pairs)-1) and len(to_encode) > 1):
                    running_total += len(to_encode)

                    n_rows = len(to_encode)
                    # get the longest segment, and add two for special tokens
                    min_len = min([len(segment) for segment in to_encode]) + 2
                    max_len = max([len(segment) for segment in to_encode]) + 2
                    print()
                    print(index_sample_i, batch, n_rows, min_len, max_len, running_total, '/', len(pairs))

                    input_ids = np.zeros([batch_size, max_len], dtype=int)
                    attention_mask = np.zeros_like(input_ids)

                    for s_i, segment in enumerate(to_encode):
                        attention_mask[s_i, 0:len(segment)+2] = 1
                        input_ids[s_i, 0:len(segment)+2] = [101] + list(tokenizer.convert_tokens_to_ids(segment)) + [102]

                    attention_mask = torch.tensor(attention_mask, dtype=torch.int64)
                    input_ids = torch.tensor(input_ids, dtype=torch.int64)

                    input_ids_on_device = input_ids.to(device)
                    attention_mask_on_device = attention_mask.to(device)

                    output_vectors_batch = []

                    # process the text through the model
                    with torch.no_grad():
                        try:
                            output_layer = model(input_ids=input_ids_on_device, attention_mask=attention_mask_on_device)[0]
                            output_layer_np = output_layer.detach().cpu().numpy()
                                
                            for row in range(n_rows):
                                target = target_word_piece_indices[row]+1
                                output_vectors_batch.append(np.array(output_layer_np[row, target, :].copy(), dtype=np.float32))

                        except Exception as e:
                            print(n_rows)
                            raise e

                        # also compute the word replacement probs according to the model
                        input_vectors = torch.tensor(np.vstack(output_vectors_batch)).to(device)
                        preds = mlm(input_vectors)
                        probs = torch.softmax(preds, dim=1)
                        probs_np = probs.detach().cpu().numpy().copy()

                    for row in range(n_rows):
                        # sort the predicted probabiliities for this row from highest to lowest
                        sub_order = np.argsort(probs_np[row, :])[::-1]

                        top_word_list = [sorted_vocab[kk] for kk in sub_order[:top_k*500]]
                        top_word_prob_list = [float(probs_np[row, sub_order[kk]]) for kk in range(top_k*500)]
                        
                        valid_indices = [kk for kk, token in enumerate(top_word_list) if len(token) > 1 and token not in stopwords and '##' not in token and '...' not in token and '[' not in token]
                        top_word_list = [top_word_list[kk] for kk in valid_indices[:top_k]]
                        top_word_prob_list = [top_word_prob_list[kk] for kk in valid_indices[:top_k]]
                        assert len(top_word_list) == top_k
                        assert len(top_word_prob_list) == top_k

                        top_words.append(top_word_list)
                        top_word_probs.append(top_word_prob_list)
                        sub_counter.update(top_word_list)

                    # clear these arrays to start the next batch
                    instances_in_batch = []
                    target_tokens = []
                    to_encode = []
                    target_word_piece_indices = []

                    batch += 1
                            
            print("Saving")
            outfile = os.path.join(outdir, target_term + '_substitutes.jsonlist')
            with open(outfile, 'w') as f:
                for jj, line_id in enumerate(line_ids_to_save):
                    f.write(json.dumps({'line_id': line_id,
                                        'token_index': int(token_indices_to_save[jj]),
                                        'top_terms': top_words[jj],
                                        'top_term_probs': top_word_probs[jj]
                                        }) + '\n')
                    

            print(target_counter.most_common(n=100))
            print(sub_counter.most_common(n=10))

if __name__ == '__main__':
    main()
