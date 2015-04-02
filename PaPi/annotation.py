# -*- coding: utf-8

# Copyright (C) 2014, A. Murat Eren
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation; either version 2 of the License, or (at your option)
# any later version.
#
# Please read the COPYING file.

"""
    Classes to create and access the annotation database.
"""

__version__ = "0.5.0"


import os
import sys
import numpy
import random
import hashlib
import operator
from collections import Counter

import PaPi.db as db
import PaPi.fastalib as u
import PaPi.utils as utils
import PaPi.kmers as kmers
import PaPi.contig as contig
import PaPi.dictio as dictio
import PaPi.terminal as terminal
import PaPi.filesnpaths as filesnpaths
import PaPi.ccollections as ccollections

from PaPi.tables import *
from PaPi.utils import ConfigError
from PaPi.commandline import HMMSearch
from PaPi.parsers import parser_modules

run = terminal.Run()
progress = terminal.Progress()


class ProfileDatabase:
    """To create an empty profile database and/or access one."""
    def __init__(self, db_path, version, run=run, progress=progress, quiet = True):
        self.db = None
        self.db_path = db_path
        self.version = version

        self.run = run
        self.progress = progress
        self.quiet = quiet

        self.init()


    def init(self):
        if os.path.exists(self.db_path):
            self.db = db.DB(self.db_path, self.version)

            self.run.info('Profile database', 'An existing database, %s, has been initiated.' % self.db_path, quiet = self.quiet)
            self.run.info('Samples', self.db.get_meta_value('samples'), quiet = self.quiet)
        else:
            self.db = None


    def create(self, meta_values = {}):
        if os.path.exists(self.db_path):
            raise ConfigError, "PaPi will not overwrite an existing profile database. Please choose a different name\
                                or remove the existing database ('%s') first." % (self.db_path)

        if not self.db_path.lower().endswith('.db'):
            raise ConfigError, "Please make sure your output file name has a '.db' extension. PaPi developers apologize\
                                for imposing their views on how local databases should be named, and are humbled by your\
                                cooperation."

        self.db = db.DB(self.db_path, self.version, new_database = True)

        for key in meta_values:
            self.db.set_meta_value(key, meta_values[key])

        # creating empty default tables
        self.db.create_table(clusterings_table_name, clusterings_table_structure, clusterings_table_types)
        self.db.create_table(gene_coverages_table_name, gene_coverages_table_structure, gene_coverages_table_types)
        self.db.create_table(variable_positions_table_name, variable_positions_table_structure, variable_positions_table_types)
        ccollections.create_blank_collections_tables(self.db)

        self.disconnect()

        self.run.info('Annotation database', 'A new database, %s, has been created.' % (self.db_path), quiet = self.quiet)


    def disconnect(self):
        self.db.disconnect()


class AnnotationDatabase:
    """To create an empty annotation database and/or access one."""
    def __init__(self, db_path, run=run, progress=progress, quiet = True):
        self.db = None
        self.db_path = db_path

        self.run = run
        self.progress = progress
        self.quiet = quiet

        self.init()


    def init(self):
        if os.path.exists(self.db_path):
            self.db = db.DB(self.db_path, __version__)

            self.run.info('Annotation database', 'An existing database, %s, has been initiated.' % self.db_path, quiet = self.quiet)
            self.run.info('Number of contigs', self.db.get_meta_value('num_contigs'), quiet = self.quiet)
            self.run.info('Number of splits', self.db.get_meta_value('num_splits'), quiet = self.quiet)
            self.run.info('Total number of nucleotides', self.db.get_meta_value('total_length'), quiet = self.quiet)
            self.run.info('Split length', self.db.get_meta_value('split_length'), quiet = self.quiet)
        else:
            self.db = None


    def create(self, contigs_fasta, split_length, kmer_size = 4):
        if os.path.exists(self.db_path):
            raise ConfigError, "PaPi will not overwrite an existing annotation database. Please choose a different name\
                                or remove the existing database ('%s') first." % (self.db_path)

        if not split_length:
            raise ConfigError, "Creating a new annotation database requires split length information to be\
                                provided. But the AnnotationDatabase class was called to create one without this\
                                bit of information. Not cool."

        if not os.path.exists(contigs_fasta):
            raise ConfigError, "Creating a new annotation database requires a FASTA file with contigs to be provided."


        if not self.db_path.lower().endswith('.db'):
            raise ConfigError, "Please make sure your output file name has a '.db' extension. PaPi developers apologize\
                                for imposing their views on how local databases should be named, and are humbled by your\
                                cooperation."

        try:
            split_length = int(split_length)
        except:
            raise ConfigError, "Split size must be an integer."

        try:
            kmer_size = int(kmer_size)
        except:
            raise ConfigError, "K-mer size must be an integer."
        if kmer_size < 2 or kmer_size > 8:
            raise ConfigError, "We like our k-mer sizes between 2 and 8, sorry! (but then you can always change the\
                                source code if you are not happy to be told what you can't do, let us know how it goes!)."

        self.db = db.DB(self.db_path, __version__, new_database = True)

        # know thyself
        self.db.set_meta_value('db_type', 'annotation')
        # this will be the unique information that will be passed downstream whenever this db is used:
        self.db.set_meta_value('annotation_hash', '%08x' % random.randrange(16**8))
        # set split length variable in the meta table
        self.db.set_meta_value('split_length', split_length)

        self.db.create_table(contig_sequences_table_name, contig_sequences_table_structure, contig_sequences_table_types)
        self.db.create_table(contig_lengths_table_name, contig_lengths_table_structure, contig_lengths_table_types)
        self.db.create_table(splits_info_table_name, splits_info_table_structure, splits_info_table_types)

        # lets process and store the FASTA file.
        fasta = u.SequenceSource(contigs_fasta)
        num_contigs, total_length = 0, 0
        db_entries_contig_sequences = []
        db_entries_contig_lengths = []
        db_entries_splits_info = []

        contigs_kmer_table = KMerTablesForContigsAndSplits('kmer_contigs', k=kmer_size)
        splits_kmer_table = KMerTablesForContigsAndSplits('kmer_splits', k=kmer_size)

        while fasta.next():
            num_contigs += 1
            contig_length = len(fasta.seq)
            chunks = utils.get_chunks(contig_length, split_length)

            contig_kmer_freq = contigs_kmer_table.get_kmer_freq(fasta.seq)

            for order in range(0, len(chunks)):
                start, end = chunks[order]
                split_name = contig.gen_split_name(fasta.id, order)
                db_entries_splits_info.append((split_name, order, start, end, fasta.id), )

                # this is very confusing, because both contigs_kmer_table and splits_kmer_able in fact
                # holds kmer values for splits only. in one table, each split has a kmer value of their
                # contigs (to not lose the genomic context while clustering based on kmers), in the other
                # one each split holds its own kmer value.
                contigs_kmer_table.append(split_name, fasta.seq[start:end], kmer_freq = contig_kmer_freq)
                splits_kmer_table.append(split_name, fasta.seq[start:end])

            db_entries_contig_sequences.append((fasta.id, fasta.seq), )
            db_entries_contig_lengths.append((fasta.id, contig_length), )
            total_length += contig_length

        self.db.set_meta_value('kmer_size', kmer_size)
        contigs_kmer_table.store(self.db)
        splits_kmer_table.store(self.db)

        self.db._exec_many('''INSERT INTO %s VALUES (?,?)''' % contig_sequences_table_name, db_entries_contig_sequences)
        self.db._exec_many('''INSERT INTO %s VALUES (?,?)''' % contig_lengths_table_name, db_entries_contig_lengths)
        self.db._exec_many('''INSERT INTO %s VALUES (?,?,?,?,?)''' % splits_info_table_name, db_entries_splits_info)

        # set some useful meta values:
        self.db.set_meta_value('num_contigs', num_contigs)
        self.db.set_meta_value('total_length', total_length)
        self.db.set_meta_value('num_splits', len(db_entries_splits_info))
        self.db.set_meta_value('genes_annotation_source', None)

        # creating empty default tables
        self.db.create_table(hmm_hits_info_table_name, hmm_hits_info_table_structure, hmm_hits_info_table_types)
        self.db.create_table(hmm_hits_splits_table_name, hmm_hits_splits_table_structure, hmm_hits_splits_table_types)
        self.db.create_table(hmm_hits_contigs_table_name, hmm_hits_contigs_table_structure, hmm_hits_contigs_table_types)
        self.db.create_table(genes_contigs_table_name, genes_contigs_table_structure, genes_contigs_table_types)
        self.db.create_table(genes_splits_summary_table_name, genes_splits_summary_table_structure, genes_splits_summary_table_types)
        self.db.create_table(genes_splits_table_name, genes_splits_table_structure, genes_splits_table_types)
        ccollections.create_blank_collections_tables(self.db)

        self.disconnect()

        self.run.info('Annotation database', 'A new database, %s, has been created.' % (self.db_path), quiet = self.quiet)
        self.run.info('Number of contigs', num_contigs, quiet = self.quiet)
        self.run.info('Total number of nucleotides', total_length, quiet = self.quiet)
        self.run.info('Split length', split_length, quiet = self.quiet)


    def disconnect(self):
        self.db.disconnect()


class KMerTablesForContigsAndSplits:
    def __init__(self, table_name, k = 4):
        self.table_name = table_name
        self.kmers_class = kmers.KMers(k)
        self.kmers = sorted(list(self.kmers_class.kmers[k]))

        self.kmer_dict = {}
        self.db_entries = []

        self.kmers_table_structure = ['contig'] + self.kmers
        self.kmers_table_types = ['text'] + ['numeric'] * len(self.kmers)


    def get_kmer_freq(self, sequence):
        return self.kmers_class.get_kmer_frequency(sequence)


    def append(self, seq_id, sequence, kmer_freq = None):
        if not kmer_freq:
            kmer_freq = self.kmers_class.get_kmer_frequency(sequence)

        db_entry = tuple([seq_id] + [kmer_freq[kmer] for kmer in self.kmers])
        self.db_entries.append(db_entry)


    def store(self, db):
        db.create_table(self.table_name, self.kmers_table_structure, self.kmers_table_types)
        db._exec_many('''INSERT INTO %s VALUES (%s)''' % (self.table_name, (','.join(['?'] * len(self.kmers_table_structure)))), self.db_entries)


class TableForVariability(Table):
    def __init__(self, db_path, version, run=run, progress=progress):
        self.db_path = db_path

        Table.__init__(self, self.db_path, version, run, progress)

        self.num_entries = 0
        self.db_entries = []
        self.set_next_available_id(variable_positions_table_name)


    def append(self, profile):
        db_entry = tuple([self.next_id(variable_positions_table_name)] + [profile[h] for h in variable_positions_table_structure[1:]])
        self.db_entries.append(db_entry)
        self.num_entries += 1
        if self.num_entries % 100 == 0:
            progress.update('Information for %d SNP sites have been added ...' % self.num_entries)


    def store(self):
        profile_db = ProfileDatabase(self.db_path, self.version)
        profile_db.db._exec_many('''INSERT INTO %s VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''' % variable_positions_table_name, self.db_entries)
        profile_db.disconnect()


class TableForGeneCoverages(Table):
    '''The purpose of this class is to keep coverage values for each gene in contigs for found in a sample.
       Simply, you create an instance from it, keep sending contig instances from contig.py::Contig class along with
       a list of inferred start/stop locations for each reading frame. Once you are done, you call create_gene_coverages_table.'''
    def __init__(self, db_path, version, run=run, progress=progress):
        self.db_path = db_path

        Table.__init__(self, self.db_path, version, run, progress)

        self.genes = []
        self.set_next_available_id(gene_coverages_table_name)

        # we keep coverage values in contig.py/Contig instances only for splits, during the profiling,
        # coverage for contigs are temporarily calculated, and then discarded. probably that behavior
        # should change for good. but for now I will generate a dict to keep contig coverages to avoid
        # even more redundant computations:
        self.contig_coverages = {}


    def analyze_contig(self, contig, sample_id, start_stop_pos_list):
        if contig.name not in self.contig_coverages:
            contig_coverage = []
            for split in contig.splits:
                contig_coverage.extend(split.coverage.c)
            self.contig_coverages[contig.name] = contig_coverage

        for prot, start, stop in start_stop_pos_list:
            gene_coverage = numpy.mean(self.contig_coverages[contig.name][start:stop])
            self.add_gene_entry(prot, sample_id, gene_coverage)


    def add_gene_entry(self, prot, sample_id, coverage):
        self.genes.append({'prot': prot, 'sample_id': sample_id, 'mean_coverage': coverage})


    def store(self):
        profile_db = ProfileDatabase(self.db_path, self.version)
        db_entries = [tuple([self.next_id(gene_coverages_table_name)] + [gene[h] for h in gene_coverages_table_structure[1:]]) for gene in self.genes]
        profile_db.db._exec_many('''INSERT INTO %s VALUES (?,?,?,?)''' % gene_coverages_table_name, db_entries)
        profile_db.disconnect()


class TablesForSearches(Table):
    def __init__(self, db_path, run=run, progress=progress):
        self.db_path = db_path

        self.debug = False

        Table.__init__(self, self.db_path, __version__, run, progress)

        self.set_next_available_id(hmm_hits_contigs_table_name)
        self.set_next_available_id(hmm_hits_splits_table_name)


    def populate_search_tables(self, sources = {}):
        if not len(sources):
            import PaPi.data.hmm
            sources = PaPi.data.hmm.sources

        if not sources:
            return

        commander = HMMSearch()
        contigs_fasta = self.export_contigs_in_db_into_FASTA_file()
        proteins_in_contigs_fasta = commander.run_prodigal(contigs_fasta)
        if not self.debug:
            os.remove(contigs_fasta)

        for source in sources:
            kind_of_search = sources[source]['kind']
            all_genes_searched_against = sources[source]['genes']
            hmm_model = sources[source]['model']
            reference = sources[source]['ref']
            hmm_scan_hits_txt = commander.run_hmmscan(source,
                                                      all_genes_searched_against,
                                                      hmm_model,
                                                      reference)

            if not hmm_scan_hits_txt:
                search_results_dict = {}
            else:
                parser = parser_modules['search']['hmmscan'](proteins_in_contigs_fasta, hmm_scan_hits_txt)
                search_results_dict = parser.get_search_results()

            self.append(source, reference, kind_of_search, all_genes_searched_against, search_results_dict)

        if not self.debug:
            commander.clean_tmp_dirs()


    def append(self, source, reference, kind_of_search, all_genes, search_results_dict):
        self.delete_entries_for_key('source', source, [hmm_hits_info_table_name, hmm_hits_contigs_table_name, hmm_hits_splits_table_name])

        annotation_db = AnnotationDatabase(self.db_path)

        # push information about this search result into serach_info table.
        db_entries = [source, reference, kind_of_search, ', '.join(all_genes)]
        annotation_db.db._exec('''INSERT INTO %s VALUES (?,?,?,?)''' % hmm_hits_info_table_name, db_entries)
        # then populate serach_data table for each contig.
        db_entries = [tuple([self.next_id(hmm_hits_contigs_table_name), source] + [v[h] for h in hmm_hits_contigs_table_structure[2:]]) for v in search_results_dict.values()]
        annotation_db.db._exec_many('''INSERT INTO %s VALUES (?,?,?,?,?,?,?,?)''' % hmm_hits_contigs_table_name, db_entries)

        db_entries = self.process_splits(source, search_results_dict)
        annotation_db.db._exec_many('''INSERT INTO %s VALUES (?,?,?,?,?,?,?)''' % hmm_hits_splits_table_name, db_entries)

        annotation_db.disconnect()


    def process_splits(self, source, search_results_dict):
        hits_per_contig = {}
        for hit in search_results_dict.values():
            if hits_per_contig.has_key(hit['contig']):
                hits_per_contig[hit['contig']].append(hit)
            else:
                hits_per_contig[hit['contig']] = [hit]

        db_entries_for_splits = []

        for contig in self.contig_lengths:
            if not hits_per_contig.has_key(contig):
                # no hits for this contig. pity!
                continue

            for split_name in self.contig_name_to_splits[contig]:
                start = self.splits[split_name]['start']
                stop = self.splits[split_name]['end']

                # FIXME: this really needs some explanation.
                for hit in hits_per_contig[contig]:
                    if hit['stop'] > start and hit['start'] < stop:
                        gene_length = hit['stop'] - hit['start']
                        # if only a part of the gene is in the split:
                        start_in_split = (start if hit['start'] < start else hit['start']) - start
                        stop_in_split = (stop if hit['stop'] > stop else hit['stop']) - start
                        percentage_in_split = (stop_in_split - start_in_split) * 100.0 / gene_length
                        
                        gene_unique_identifier = hashlib.sha224('_'.join([contig, hit['gene_name'], str(hit['start']), str(hit['stop'])])).hexdigest()
                        db_entry = tuple([self.next_id(hmm_hits_splits_table_name), source, gene_unique_identifier, hit['gene_name'], split_name, percentage_in_split, hit['e_value']])
                        db_entries_for_splits.append(db_entry)

        return db_entries_for_splits


class TablesForGenes(Table):
    def __init__(self, db_path, run=run, progress=progress):
        self.db_path = db_path

        Table.__init__(self, self.db_path, __version__, run, progress)

        # this class keeps track of genes that occur in splits, and responsible
        # for generating the necessary table in the annotation database
        self.genes_in_splits = GenesInSplits()


    def create(self, genes_dict, parser):
        self.genes_dict = genes_dict

        self.sanity_check()

        # oepn connection
        annotation_db = AnnotationDatabase(self.db_path)

        # test whether there are already genes tables populated
        genes_annotation_source = annotation_db.db.get_meta_value('genes_annotation_source')
        if genes_annotation_source:
            self.run.warning('Previous genes annotation data from "%s" will be replaced with the incoming data' % parser)
            annotation_db.db._exec('''DELETE FROM %s''' % (genes_contigs_table_name))
            annotation_db.db._exec('''DELETE FROM %s''' % (genes_splits_table_name))
            annotation_db.db._exec('''DELETE FROM %s''' % (genes_splits_summary_table_name))

        # set the parser
        annotation_db.db.remove_meta_key_value_pair('genes_annotation_source')
        annotation_db.db.set_meta_value('genes_annotation_source', parser)
        # push raw entries
        db_entries = [tuple([prot] + [self.genes_dict[prot][h] for h in genes_contigs_table_structure[1:]]) for prot in self.genes_dict]
        annotation_db.db._exec_many('''INSERT INTO %s VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''' % genes_contigs_table_name, db_entries)
        # disconnect like a pro.
        annotation_db.disconnect()


        # compute and push split taxonomy information.
        self.init_genes_splits_summary_table()


    def sanity_check(self):
        # check whether input matrix dict 
        keys_found = ['prot'] + self.genes_dict.values()[0].keys()
        missing_keys = [key for key in genes_contigs_table_structure if key not in keys_found]
        if len(missing_keys):
            raise ConfigError, "Your input lacks one or more header fields to generate a PaPi annotation db. Here is\
                                what you are missing: %s. The complete list (and order) of headers in your TAB\
                                delimited matrix file (or dictionary) must follow this: %s." % (', '.join(missing_keys),
                                                                                                ', '.join(genes_contigs_table_structure))


        contig_names_in_matrix = set([v['contig'] for v in self.genes_dict.values()])
        contig_names_in_db  = set(self.contig_lengths.keys())

        for contig in contig_names_in_matrix:
            if contig not in contig_names_in_db:
                raise ConfigError, "We have a problem... Every contig name found in the input file you provide\
                                    must be found in the annotation database. But it seems it is not the case. I did not check\
                                    all, but there there is at least one contig name ('%s') that appears in your\
                                    matrices, but missing in the database. You may need to format the contig\
                                    names in your FASTA file and regenerate the annotation database to match contig\
                                    names appear in your matrices. Keep in mind that contig names must also match the\
                                    ones in your BAM files later on. Even when you use one software for assembly and\
                                    mapping, disagreements between contig names may arise. We know that it is the case\
                                    with CLC for instance. OK. Going back to the issue. Here is one contig name from\
                                    the annotation database (which was originally in your contigs FASTA): '%s', and\
                                    here is one from your input files you just provided: '%s'. You should make them\
                                    identical (and make sure whatever solution you come up with will not make them\
                                    incompatible with names in your BAM files later on. Sorry about this mess, but\
                                    there is nothing much PaPi can do about this issue." %\
                                                    (contig, contig_names_in_db.pop(), contig_names_in_matrix.pop())


    def init_genes_splits_summary_table(self):
        # build a dictionary for fast access to all proteins identified within a contig
        prots_in_contig = {}
        for prot in self.genes_dict:
            contig = self.genes_dict[prot]['contig']
            if prots_in_contig.has_key(contig):
                prots_in_contig[contig].add(prot)
            else:
                prots_in_contig[contig] = set([prot])

        contigs_without_annotation = list(set(self.contig_lengths.keys()) - set(prots_in_contig.keys()))
        run.info('Percent of contigs annotated', '%.1f%%' % (len(prots_in_contig) * 100.0 / len(self.contig_lengths)))

        for contig in contigs_without_annotation:
            prots_in_contig[contig] = set([])

        splits_dict = {}
        split_to_prot = {}
        for contig in self.contig_lengths:
            for split_name in self.contig_name_to_splits[contig]:
                start = self.splits[split_name]['start']
                stop = self.splits[split_name]['end']

                taxa = []
                functions = []
                gene_start_stops = []
                # here we go through all genes in the contig and identify the all the ones that happen to be in
                # this particular split to generate summarized info for each split. BUT one important that is done
                # in the following loop is self.genes_in_splits.add call, which populates GenesInSplits class.
                for prot in prots_in_contig[contig]:
                    if self.genes_dict[prot]['stop'] > start and self.genes_dict[prot]['start'] < stop:
                        taxa.append(self.genes_dict[prot]['t_species'])
                        functions.append(self.genes_dict[prot]['function'])
                        gene_start_stops.append((self.genes_dict[prot]['start'], self.genes_dict[prot]['stop']), )
                        self.genes_in_splits.add(split_name, start, stop, prot, self.genes_dict[prot]['start'], self.genes_dict[prot]['stop'])


                taxonomy_strings = [t for t in taxa if t]
                function_strings = [f for f in functions if f]

                # here we identify genes that are associated with a split even if one base of the gene spills into 
                # the defined start or stop of a split, which means, split N, will include genes A, B and C in this
                # scenario:
                #
                # contig: (...)------[ gene A ]--------[     gene B    ]----[gene C]---------[    gene D    ]-----(...)
                #         (...)----------x---------------------------------------x--------------------------------(...)
                #                        ^ (split N start)                       ^ (split N stop)
                #                        |                                       |
                #                        |<-              split N              ->|
                #
                # however, when looking at the coding versus non-coding nucleotide ratios in a split, we have to make
                # sure that only the relevant portion of gene A and gene C is counted:
                total_coding_nts = 0
                for gene_start, gene_stop in gene_start_stops:
                    total_coding_nts += (gene_stop if gene_stop < stop else stop) - (gene_start if gene_start > start else start)

                splits_dict[split_name] = {'taxonomy': None,
                                           'num_genes': len(taxa),
                                           'avg_gene_length': numpy.mean([(l[1] - l[0]) for l in gene_start_stops]) if len(gene_start_stops) else 0.0,
                                           'ratio_coding': total_coding_nts * 1.0 / (stop - start),
                                           'ratio_hypothetical': (len(functions) - len(function_strings)) * 1.0 / len(functions) if len(functions) else 0.0,
                                           'ratio_with_tax': len(taxonomy_strings) * 1.0 / len(taxa) if len(taxa) else 0.0,
                                           'tax_accuracy': 0.0}
                distinct_taxa = set(taxonomy_strings)

                if not len(distinct_taxa):
                    continue

                if len(distinct_taxa) == 1:
                    splits_dict[split_name]['taxonomy'] = distinct_taxa.pop()
                    splits_dict[split_name]['tax_accuracy'] = 1.0
                else:
                    d = Counter()
                    for taxon in taxonomy_strings:
                        d[taxon] += 1
                    consensus, occurrence = sorted(d.items(), key=operator.itemgetter(1))[-1]
                    splits_dict[split_name]['taxonomy'] = consensus
                    splits_dict[split_name]['tax_accuracy'] = occurrence * 1.0 / len(taxonomy_strings)

        # open connection
        annotation_db = AnnotationDatabase(self.db_path)
        # push raw entries for splits table
        db_entries = [tuple([split] + [splits_dict[split][h] for h in genes_splits_summary_table_structure[1:]]) for split in splits_dict]
        annotation_db.db._exec_many('''INSERT INTO %s VALUES (?,?,?,?,?,?,?,?)''' % genes_splits_summary_table_name, db_entries)
        # push entries for genes in splits table
        db_entries = [tuple([entry_id] + [self.genes_in_splits.splits_to_prots[entry_id][h] for h in genes_splits_table_structure[1:]]) for entry_id in self.genes_in_splits.splits_to_prots]
        annotation_db.db._exec_many('''INSERT INTO %s VALUES (?,?,?,?,?,?)''' % genes_splits_table_name, db_entries)
        # disconnect
        annotation_db.disconnect()


    def get_consensus_taxonomy_for_split(self, contig, t_level = 't_species', start = 0, stop = sys.maxint):
        """Returns (c, n, t, o) where,
            c: consensus taxonomy (the most common taxonomic call for each gene found in the contig),
            n: total number of genes found in the contig,
            t: total number of genes with known taxonomy,
            o: number of taxonomic calls that matches the consensus among t
        """

        response = self.db.cursor.execute("""SELECT %s FROM %s WHERE contig='%s' and stop > %d and start < %d""" % (t_level, genes_contigs_table_name, contig, start, stop))
        rows = response.fetchall()

        num_genes = len(rows)
        tax_str_list = [t[0] for t in rows if t[0]]
        distinct_taxa = set(tax_str_list)

        if not len(distinct_taxa):
            return None, num_genes, 0, 0

        if len(distinct_taxa) == 1:
            return distinct_taxa.pop(), num_genes, len(tax_str_list), len(tax_str_list)
        else:
            d = Counter()
            for t in tax_str_list:
                d[t] += 1
            consensus, occurrence = sorted(d.items(), key=operator.itemgetter(1))[-1]
            return consensus, num_genes, len(tax_str_list), occurrence


class GenesInSplits:
    def __init__(self):
        self.entry_id = 0
        self.splits_to_prots = {}

    def add(self, split_name, split_start, split_end, prot_id, prot_start, prot_end):

        gene_length = prot_end - prot_start

        if gene_length <= 0:
            raise ConfigError, "annotation.py/GeneInSplits: OK. There is something wrong. We have this gene, '%s',\
                                which starts at position %d and ends at position %d. Well, it doesn't look right,\
                                does it?" % (prot_id, prot_start, prot_end)

        # if only a part of the gene is in the split:
        start_in_split = (split_start if prot_start < split_start else prot_start) - split_start
        stop_in_split = (split_end if prot_end > split_end else prot_end) - split_start
        percentage_in_split = (stop_in_split - start_in_split) * 100.0 / gene_length

        self.splits_to_prots[self.entry_id] = {'split': split_name,
                                               'prot': prot_id,
                                               'start_in_split': start_in_split,
                                               'stop_in_split': stop_in_split,
                                               'percentage_in_split': percentage_in_split}
        self.entry_id += 1

