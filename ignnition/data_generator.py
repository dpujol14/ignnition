'''
 *
 * Copyright (C) 2020 Universitat Politècnica de Catalunya.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at:
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
'''

# -*- coding: utf-8 -*-


import glob
import json
import sys
import tarfile
import numpy as np
import math
import random
import tensorflow as tf
import json
import warnings
from ignnition.utils import *


class Generator:
    def __init__(self):
        self.end_symbol = bytes(']', 'utf-8')

    def __make_indices(self, sample, entity_names):
        """
        Parameters
        ----------
        entities:    dict
           Dictionary with the information of each entity
        """
        counter = {}
        indices = {}
        for e in entity_names:
            items = sample[e]
            indices[e] = {}
            counter[e] = 0
            for node in items:
                indices[e][node] = counter[e]
                counter[e] += 1

        return counter, indices

    def stream_read_json(self, f):
        start_pos = 1
        while True:
            try:
                obj = json.load(f)
                yield obj
                return
            except json.JSONDecodeError as e:
                f.seek(start_pos)
                json_str = f.read(e.pos)
                obj = json.loads(json_str)
                start_pos += e.pos + 1
                a = f.read(1)  # this 1 is the coma or the final symbol

                if a == self.end_symbol or a == ']':
                    yield obj
                    return
                yield obj

    def __process_sample(self, sample, file=None):
        data = {}
        output = []
        # check that all the entities are defined
        for e in self.entity_names:
            if e not in sample:
                if file is not None:
                    if file is not None:
                        print_info("Error in the dataset file located in '" + file + ".")
                    print_info("The entity '" + e + "' was used in the model_description.json file "
                                                     "but was not defined in the dataset. A list should be defined with the names (string) of each node of type " + e + '.\n'
                                                                                                                                                                        'E.g., "' + e + '": ["n1", "n2", "n3" ...]')

        # read the features
        for f in self.feature_names:
            if f not in sample:
                if file is not None:
                    print_info("Error in the dataset file located in '" + file + ".")
                print_info('The feature "' + f + '" was used in the model_description.yaml file '
                                                     'but was not defined in the dataset. A list should be defined with the corresponding value'
                                                     'for each of the nodes of its entity type. \n'
                                                     'E.g., "' + f + '": [v1, v2, v3 ...]')
            else:
                data[f] = sample[f]

        # read additional input name
        for a in self.additional_input:
            if a not in sample:
                if file is not None:
                    print_info("Error in the dataset file located in '" + file + ".")
                print_info('There was an list of float values named"' + str(
                    a) + '" which was used in the model_description.yaml file. Thus it must be defined in the dataset. Please make sure the spelling is correct.')
            else:
                data[a] = sample[a]

        # read the output values if we are training
        if self.training:
            if self.output_name not in sample:
                if file is not None:
                    print_info("Error in the dataset file located in '" + file + ".")
                print('The model_description.yaml file defined an output_label named "' + self.output_name + '". This was, however, not found. Make sure the spelling is correct, and that it is a valid array of float values.')
            else:
                value = sample[self.output_name]
                if not isinstance(value, list):
                    value = [value]

                output += value

        num_nodes, indices = self.__make_indices(sample, self.entity_names)

        # create the adjacencies
        for a in self.adj_names:
            name, src_entity, dst_entity, uses_parameters = a

            if name not in sample:
                if file is not None:
                    print_info("Error in the dataset file located in '" + file + ".")
                print_info('A list for the adjecency list named "' + name + '" was not found although being expected.\n'
                                                                     'Remember that an adjacecy list connecting entity "a" to "b" should be defined as follows:\n'
                                                                     '{"b1":["a1","a2",...], "b2":["a2","a4"...]')
            else:
                adjacency_lists = sample[name]  # this is the list defining the adjacency_list
                src_idx, dst_idx, seq, parameters = [], [], [], []

                # obtain the indices defined by the names of each of the entities
                try:
                    indices_src = indices[src_entity]
                except:
                    if file is not None:
                        print_info("Error in the dataset file located in '" + file + ".")
                        print_info('The adjecency list "' + name + '" was expected to be from ' + src_entity + ' to ' + dst_entity +
                             '.\n However the source entity defined in the dataset does not match')

                try:
                    indices_dst = indices[dst_entity]
                except:
                    if file is not None:
                        print_info("Error in the dataset file located in '" + file + ".")
                        print_info('The adjecency list "' + name + '" was expected to be from ' + src_entity + ' to ' + dst_entity +
                        '.\n However the destination entity defined in the dataset does not match')

                # The structure of the adjacency_list is p1: [[source_node, dst_node, parameters], []...] (parameters is optional)
                counters = {}   #this is useful to calculate the sequence order
                for edge in adjacency_lists:
                    src_idx.append(indices_src[edge[0]])
                    dst_idx.append(indices_dst[edge[1]])
                    if len(edge) == 3:
                        parameters.append(edge[1])

                    if edge[1] not in counters:
                        counters[edge[1]] = 0

                    seq.append(counters[edge[1]])
                    counters[edge[1]] += 1

                data['src_' + name] = src_idx
                data['dst_' + name] = dst_idx
                data['seq_' + src_entity + '_' + dst_entity] = seq

                # remains to check that all adjacencies of the same type have params or not (not a few of them)!!!!!!!!!!
                if parameters != []:
                     data['params_' + name] = parameters

        # define the graph nodes
        items = num_nodes.items()
        for entity, n_nodes in items:
            data['num_' + entity] = n_nodes

        # obtain the sequence for the combined message passing. One for each source entity sending to the destination.
        for i in self.interleave_names:
            name, dst_entity = i
            interleave_definition = sample[name]

            involved_entities = {}
            total_sequence = []
            total_size, n_total, counter = 0, 0, 0

            for entity in interleave_definition:
                total_size += 1
                if entity not in involved_entities:
                    involved_entities[entity] = counter  # each entity a different value (identifier)

                    seq = data['seq_' + entity + '_' + dst_entity]
                    n_total += max(seq) + 1  # superior limit of the size of any destination
                    counter += 1

                # obtain all the original definition in a numeric format
                total_sequence.append(involved_entities[entity])

            # we exceed the maximum length for sake to make it multiple. Then we will cut it
            repetitions = math.ceil(float(n_total) / total_size)
            result = np.array((total_sequence * repetitions)[:n_total])

            for entity in involved_entities:
                id = involved_entities[entity]
                data['indices_' + entity + '_to_' + dst_entity] = np.where(result == id)[0].tolist()
        if self.training:
            return data, output
        else:
            return data

    def generate_from_array(self,
                            data_samples,
                            entity_names,
                            feature_names,
                            output_name,
                            adj_names,
                            interleave_names,
                            additional_input,
                            training,
                            shuffle=False):
        """
        Parameters
        ----------
        dir:    str
           Name of the entity
        feature_names:    str
           Name of the features to be found in the dataset
        output_names:    str
           Name of the output data to be found in the dataset
        adj_names:    [array]
           CHECK
        interleave_names:    [array]
           First parameter is the name of the interleave, and the second the destination entity
        predict:     bool
            Indicates if we are making predictions, and thus no label is required.
        shuffle:    bool
           Shuffle parameter of the dataset

        """
        data_samples = [json.loads(x) for x in data_samples]
        self.entity_names = [x for x in entity_names]
        self.feature_names = [x for x in feature_names]
        self.output_name = output_name
        self.adj_names = [[x[0], x[1], x[2], x[3]] for x in adj_names]
        self.interleave_names = [[i[0], i[1]] for i in interleave_names]
        self.additional_input = [x for x in additional_input]
        self.training = training
        samples = glob.glob(str(dir) + '/*.tar.gz')

        if shuffle == True:
            random.shuffle(samples)

        for sample in data_samples:
            try:
                processed_sample = self.__process_sample(sample)
                yield processed_sample

            except StopIteration:
                pass

            except KeyboardInterrupt:
                sys.exit

            except Exception as inf:
                print_info("\n There was an unexpected error: \n" + str(inf))
                print_info('Please make sure that all the names used in the sample passed ')

            sys.exit

    def generate_from_dataset(self,
                              dir,
                              entity_names,
                              feature_names,
                              output_name,
                              adj_names,
                              interleave_names,
                              additional_input,
                              training,
                              shuffle=False):
        """
        Parameters
        ----------
        dir:    str
           Name of the entity
        feature_names:    str
           Name of the features to be found in the dataset
        output_names:    str
           Name of the output data to be found in the dataset
        adj_names:    [array]
           CHECK
        interleave_names:    [array]
           First parameter is the name of the interleave, and the second the destination entity
        predict:     bool
            Indicates if we are making predictions, and thus no label is required.
        shuffle:    bool
           Shuffle parameter of the dataset

        """
        self.entity_names = entity_names
        self.feature_names = feature_names
        self.output_name = output_name
        self.adj_names = adj_names
        self.interleave_names = interleave_names
        self.additional_input = additional_input
        self.training = training

        files = glob.glob(str(dir) + '/*.json') + glob.glob(str(dir) + '/*.tar.gz')

        # no elements found
        if files == []:
            raise Exception('The dataset located in  ' + dir + ' seems to contain no valid elements (json or .tar.gz)')

        if shuffle:
            random.shuffle(files)

        for sample_file in files:
            try:
                if 'tar.gz' in sample_file:
                    tar = tarfile.open(sample_file, 'r:gz')  # read the tar files
                    try:
                        file_samples = tar.extractfile('data.json')
                    except:
                        raise Exception('The file data.json was not found in ', sample_file)

                else:
                    file_samples = open(sample_file, 'r')

                file_samples.read(1)
                data = self.stream_read_json(file_samples)
                while True:
                    processed_sample = self.__process_sample(next(data), sample_file)
                    yield processed_sample

            except StopIteration:
                pass

            except KeyboardInterrupt:
                sys.exit

            except Exception as inf:
                print_info("\n There was an unexpected error: \n" + str(inf))
                print_info('Please make sure that all the names used in the file ' + sample_file +
                           'are defined in your dataset')

                sys.exit
