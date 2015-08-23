# -*- coding: utf-8
"""The library to focus on a single bin or multiple bins.

The default client of this library is bin/anvi-refine-bin."""


import os
import textwrap

import anvio
import anvio.dbops as dbops
import anvio.terminal as terminal
import anvio.constants as constants
import anvio.clustering as clustering
import anvio.interactive as interactive
import anvio.ccollections as ccollections

from anvio.errors import RefineError
from anvio.clusteringconfuguration import ClusteringConfiguration


__author__ = "A. Murat Eren"
__copyright__ = "Copyright 2015, The anvio Project"
__credits__ = []
__license__ = "GPL 3.0"
__version__ = anvio.__version__
__maintainer__ = "A. Murat Eren"
__email__ = "a.murat.eren@gmail.com"
__status__ = "Development"


pp = terminal.pretty_print
P = lambda x, y: os.path.join(x['output_dir'], y)


class RefineBins(dbops.DatabasesMetaclass):
    def __init__(self, args):
        self.progress = terminal.Progress()
        self.run = terminal.Run()
        self.args = args

        self.bins = set([])

        A = lambda x: args.__dict__[x] if args.__dict__.has_key(x) else None
        self.bin_ids_file_path = A('bin_ids_file')
        self.bin_id = A('bin_id')
        self.collection_id = A('collection_id')
        self.annotation_db_path = A('annotation_db')
        self.profile_db_path = A('profile_db')
        self.debug = A('debug')

        self.clustering_configs = constants.clustering_configs['merged']
        self.database_paths = {'ANNOTATION.db': self.annotation_db_path,
                               'PROFILE.db': self.profile_db_path}
        self.split_names_of_interest = set([])


    def init(self):
        # get split names
        d = ccollections.GetSplitNamesInBins(self.args).get_dict()
        self.bins = d.keys()
        for split_names in d.values():
            self.split_names_of_interest.update(split_names)

        # if the user updates the refinement of a single bin or bins, there shouldn't be multiple copies
        # of that stored in the database. so everytime 'store_refined_bins' function is called,
        # it will check this varlable and, (1) if empty, continue updating stuff in db store updates
        # in it, (2) if not empty, remove items stored in this variable from collections dict, and continue
        # with step (1). the starting point is of course self.bins. when the store_refined_bins function is
        # called the first time, it will read collection data for collection_id, and remove the bin(s) in
        # analysis from it before it stores the data:
        self.ids_for_already_refined_bins = self.bins

        self.input_directory = os.path.dirname(os.path.abspath(self.profile_db_path))

        self.run.info('Input directory', self.input_directory)
        self.run.info('Collection ID', self.collection_id)
        self.run.info('Number of bins', len(self.bins))
        self.run.info('Number of splits', len(self.split_names_of_interest))


    def refine(self):
        self.init()

        clusterings = self.cluster_splits_of_interest()

        d = interactive.InputHandler(self.args, external_clustering = {'clusterings': clusterings, 'default_clustering': 'tnf-cov'})

        # set a more appropriate title
        bins = sorted(list(self.bins))
        title = 'Refining %s%s from "%s"' % (', '.join(bins[0:3]),
                                              ' (and %d more)' % (len(bins) - 3) if len(bins) > 3 else '',
                                              self.collection_id)
        d.title = textwrap.fill(title)

        return d


    def store_refined_bins(self, refined_bin_data, refined_bin_colors):
        if 0 in [len(b) for b in refined_bin_data.values()]:
            raise RefineError, 'One or more of your bins have zero splits. If you are trying to remove this bin from your collection,\
                                this is not the right way to do it.'

        self.progress.new('Storing refined bins')
        self.progress.update('accessing to collection "%s" ...' % self.collection_id)
        collection_dict = self.collections.get_collection_dict(self.collection_id)
        colors_dict = self.collections.get_collection_colors(self.collection_id)
        self.progress.end()

        bad_bin_names = [b for b in collection_dict if (b in refined_bin_data and b not in self.ids_for_already_refined_bins)]
        if len(bad_bin_names):
            raise RefineError, '%s of your bin names %s NOT unique, and already exist%s in the database. You must rename\
                                %s to something else: %s' % (
                                                              'One' if len(bad_bin_names) == 1 else len(bad_bin_names),
                                                              'is' if len(bad_bin_names) == 1 else 'are',
                                                              's' if len(bad_bin_names) == 1 else '',
                                                              'this one' if len(bad_bin_names) == 1 else 'these',
                                                              ', '.join(bad_bin_names)
                                                             )

        # remove bins that should be updated in the database:
        for bin_id in self.ids_for_already_refined_bins:
            collection_dict.pop(bin_id)
            colors_dict.pop(bin_id)

        # zero it out
        self.ids_for_already_refined_bins = set([])

        if self.debug:
            self.run.info('collection from db', collection_dict)
            self.run.info('colors from db', colors_dict)
            self.run.info_single('')

            self.run.info('incoming collection data', refined_bin_data)
            self.run.info('incoming collection colors', refined_bin_colors)
            self.run.info_single('')

        for bin_id in refined_bin_data:
            collection_dict[bin_id] = refined_bin_data[bin_id]
            colors_dict[bin_id] = refined_bin_colors[bin_id]
            self.ids_for_already_refined_bins.add(bin_id)


        if self.debug:
            self.run.info('resulting collection', collection_dict)
            self.run.info('resulting collection colors', colors_dict)
            self.run.info_single('')

        collections = dbops.TablesForCollections(self.profile_db_path, anvio.__profile__version__)
        collections.append(self.collection_id, collection_dict, colors_dict)

        self.run.info_single('"%s" collection is updated!' % self.collection_id)


    def cluster_splits_of_interest(self):
        # clustering of contigs is done for each configuration file under static/clusterconfigs/merged directory;
        # at this point we don't care what those recipes really require because we already merged and generated
        # any data that may be required.
        clusterings = {}

        for config_name in self.clustering_configs:
            config_path = self.clustering_configs[config_name]

            config = ClusteringConfiguration(config_path, self.input_directory, db_paths = self.database_paths, row_ids_of_interest = self.split_names_of_interest)

            try:
                newick = clustering.order_contigs_simple(config, progress = self.progress)
            except Exception as e:
                self.run.warning('Clustering has failed for "%s": "%s"' % (config_name, e))
                self.progress.end()
                continue

            clusterings[config_name] = {'newick': newick}

        self.run.info('available_clusterings', clusterings.keys())
        self.run.info('default_clustering', constants.merged_default)

        return clusterings
