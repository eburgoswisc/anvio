# -*- coding: utf-8
# pylint: disable=line-too-long

"""Classes to make sense of genes and variability within the context of protein structure"""

import os
import time
import shutil

import anvio
import anvio.db as db
import anvio.tables as t
import anvio.fastalib as u
import anvio.utils as utils
import anvio.dbops as dbops
import anvio.terminal as terminal
import anvio.constants as constants
import anvio.filesnpaths as filesnpaths
import anvio.drivers.MODELLER as MODELLER

import numpy as np
import pandas as pd

from collections import OrderedDict
from anvio.errors import ConfigError
from Bio.PDB import PDBParser
from Bio.PDB import DSSP


class StructureDatabase(object):
    def __init__(self,
                 file_path,
                 db_hash=None,
                 residue_info_structure_extras=[],
                 residue_info_types_extras=[],
                 create_new=False,
                 ignore_hash=False,
                 run=terminal.Run(),
                 progress=terminal.Progress(),
                 quiet=False):

        self.db_type     = 'structure'
        self.db_hash     = str(db_hash)
        self.version     = anvio.__structure__version__
        self.file_path   = file_path
        self.quiet       = quiet
        self.run         = run
        self.progress    = progress
        self.table_names = None

        if not db_hash and create_new:
            raise ConfigError("You cannot create a Structure DB without supplying a DB hash.")

        self.db = db.DB(self.file_path, self.version, new_database = create_new)

        if create_new:
            # structure of the residue info table depend on annotation sources used
            self.residue_info_structure, self.residue_info_types = self.get_residue_info_table_structure(residue_info_structure_extras, residue_info_types_extras)
            self.table_names = self.create_tables()
        else:
            self.db_hash = str(self.db.get_meta_value('contigs_db_hash'))

            self.genes_with_structure = [int(x) for x in self.db.get_meta_value('genes_with_structure').split(',') if not x == '']
            self.genes_without_structure = [int(x) for x in self.db.get_meta_value('genes_without_structure').split(',') if not x == '']
            self.all_genes = self.genes_with_structure + self.genes_without_structure

        if not ignore_hash:
            self.check_hash()

        # entries initialized as empty list are added with insert_many()
        # entries initialized as empty DataFrame are added with insert_rows_from_dataframe()
        self.entries = {
            t.structure_pdb_data_table_name     : [],
            t.structure_residue_info_table_name : pd.DataFrame({}),
            t.structure_templates_table_name    : pd.DataFrame({}),
            t.structure_models_table_name       : pd.DataFrame({}),
            }


    def get_residue_info_table_structure(self, residue_info_structure_extras, residue_info_types_extras):
        """
        The structure (i.e. column numbers and labels) of the residue_info table depend on
        annotation sources used, and are taken from residue_info_structure_extras.
        """
        # If residue_info_structure_extras was sloppily passed to this class, it may have
        # some items already in t.structure_residue_info_table_name. So we delete them if they exist
        indices_to_del = [residue_info_structure_extras.index(x) for x in residue_info_structure_extras \
                                                                     if x in t.structure_residue_info_table_structure]
        for index in indices_to_del:
            del residue_info_structure_extras[index]
            del residue_info_types_extras[index]

        residue_info_structure = t.structure_residue_info_table_structure + residue_info_structure_extras
        residue_info_types = t.structure_residue_info_table_types + residue_info_types_extras
        return residue_info_structure, residue_info_types


    def create_tables(self):
        self.db.set_meta_value('db_type', self.db_type)
        self.db.set_meta_value('contigs_db_hash', self.db_hash)
        self.db.set_meta_value('creation_date', time.time())

        self.db.create_table(t.structure_pdb_data_table_name, t.structure_pdb_data_table_structure, t.structure_pdb_data_table_types)
        self.db.create_table(t.structure_templates_table_name, t.structure_templates_table_structure, t.structure_templates_table_types)
        self.db.create_table(t.structure_models_table_name, t.structure_models_table_structure, t.structure_models_table_types)
        self.db.create_table(t.structure_residue_info_table_name, self.residue_info_structure, self.residue_info_types)

        table_names = [t.structure_pdb_data_table_name,
                       t.structure_templates_table_name,
                       t.structure_models_table_name,
                       t.structure_residue_info_table_name]
        return table_names


    def check_hash(self):
        actual_db_hash = str(self.db.get_meta_value('contigs_db_hash'))
        if self.db_hash != actual_db_hash:
            raise ConfigError('The hash value inside Structure Database "%s" does not match with Contigs Database hash "%s",\
                               these files probably belong to different projects.' % (actual_db_hash, self.db_hash))


    def store(self, table_name, key=None):
        rows_data = self.entries[table_name]

        if type(rows_data) == list:
            self.db.insert_many(table_name, entries=rows_data)
            self.entries[table_name] = []

        elif type(rows_data) == pd.core.frame.DataFrame:
            self.db.insert_rows_from_dataframe(table_name, rows_data, raise_if_no_columns=False, key=key)
            self.entries[table_name] = pd.DataFrame({})

        else:
            raise ConfigError("store :: rows_data must be either a list of tuples or a pandas dataframe.")


    def get_summary_for_interactive(self, corresponding_gene_call):
        summary = {}

        print(corresponding_gene_call, self.genes_with_structure)
        if not corresponding_gene_call in self.genes_with_structure:
            raise ConfigError('gene call not found.')

        summary['pdb_content'] = self.db.get_single_column_from_table(t.structure_pdb_data_table_name, 
            'pdb_content', where_clause="corresponding_gene_call = %d" % corresponding_gene_call)[0].decode('utf-8')

        summary['residue_info'] = self.db.get_some_rows_from_table(t.structure_residue_info_table_name, 
            "corresponding_gene_call = %d" % corresponding_gene_call)

        return summary



    def disconnect(self):
        self.db.disconnect()


class Structure(object):

    def __init__(self, args, run=terminal.Run(), progress=terminal.Progress()):
        self.args = args
        self.run = run
        self.progress = progress

        # initialize self.arg parameters
        A                            = lambda x, t: t(args.__dict__[x]) if x in self.args.__dict__ else None
        null                         = lambda x: x
        self.contigs_db_path         = A('contigs_db', null)
        self.genes_of_interest_path  = A('genes_of_interest', null)
        self.splits_of_interest_path = A('splits_of_interest', null)
        self.bin_id                  = A('bin_id', null)
        self.collection_name         = A('collection_name', null)
        self.gene_caller_ids         = A('gene_caller_ids', null)
        self.output_db_path          = A('output_db_path', null)
        self.full_modeller_output    = A('dump_dir', null)
        self.skip_DSSP               = A('skip_DSSP', bool)
        self.DSSP_executable         = None

        contigs_db                   = dbops.ContigsDatabase(self.contigs_db_path)
        contigs_db_hash              = contigs_db.meta['contigs_db_hash']

        # MODELLER params
        self.modeller_database       = A('modeller_database', null)
        self.scoring_method          = A('scoring_method', null)
        self.max_matches             = A('max_number_templates', null)
        self.min_proper_pident       = A('percent_identical_cutoff', null)
        self.num_models              = A('num_models', null)
        self.deviation               = A('deviation', null)
        self.very_fast               = A('very_fast', bool)

        # check outputs are writable
        filesnpaths.is_output_file_writable(self.output_db_path)
        if self.full_modeller_output:
            self.full_modeller_output = filesnpaths.check_output_directory(self.full_modeller_output, ok_if_exists=False)

        # identify which genes user wants to model structures for
        self.get_genes_of_interest()

        self.sanity_check()

        # residue annotation
        self.annotation_sources_info = self.get_annotation_sources_info()
        self.residue_info_table_structure, self.residue_info_table_types = self.get_residue_info_table_structure()
        self.res_annotation_df = pd.DataFrame({})

        # initialize StructureDatabase
        self.structure_db = StructureDatabase(self.output_db_path,
                                              contigs_db_hash,
                                              residue_info_structure_extras = self.residue_info_table_structure,
                                              residue_info_types_extras = self.residue_info_table_types,
                                              create_new=True)


    def get_residue_info_table_structure(self):
        """
        Table structure is dependent on which annotation sources are available or of interest.
        That's why it is defined on the fly when db is created. To generate on the fly, the columns
        from each source are added, but only if skip=False for the annotation source.  codon_order_in_gene
        is ignored Since it is common to each annotation source and is already present in
        t.structure_residue_info_table_structure.
        """
        structure = []
        types = []

        for source, info in self.annotation_sources_info.items():
            if not info["skip"]:
                structure.extend(list(info["structure"].keys()))
                types.extend([info["structure"][x] for x in info["structure"].keys()])
        return structure, types


    def get_annotation_sources_info(self):
        """
        The annotation_sources_info is a dictionary spelling out all column names relevant to each
        annotation source, the method which returns the annotation dataframe, and the boolean
        stating whether or not the annotation source will be called.
        """
        annotation_sources_info = {
            "DSSP": {
                "method"    : self.run_DSSP,
                "skip"      : self.skip_DSSP,
                "structure" : {"codon_order_in_gene"   : "integer",
                               "aa"              : "text",
                               "sec_struct"      : "text",
                               "rel_solvent_acc" : "real",
                               "phi"             : "real",
                               "psi"             : "real",
                               "NH_O_1_index"    : "integer",
                               "NH_O_1_energy"   : "real",
                               "O_NH_1_index"    : "integer",
                               "O_NH_1_energy"   : "real",
                               "NH_O_2_index"    : "integer",
                               "NH_O_2_energy"   : "real",
                               "O_NH_2_index"    : "integer",
                               "O_NH_2_energy"   : "real"},
                },
            "STRIDE": {
                "method"  : lambda *args, **kwargs: None,
                "skip"    : True,
                "columns" : {
                            },
                },
            }
        return annotation_sources_info


    def sanity_check(self):

        # check for genes that do not appear in the contigs database
        bad_gene_caller_ids = [g for g in self.genes_of_interest if g not in self.genes_in_database]
        if bad_gene_caller_ids:
            raise ConfigError(("This gene caller id you provided is" if len(bad_gene_caller_ids) == 1 else \
                               "These gene caller ids you provided are") + " not known to this contigs database: {}.\
                               You have only 2 lives left. 2 more mistakes, and anvi'o will automatically uninstall \
                               itself. Yes, seriously :(".format(", ".join([str(x) for x in bad_gene_caller_ids])))

        # Finally, raise warning if number of genes is greater than 20
        if len(self.genes_of_interest) > 20:
            self.run.warning("Modelling protein structures is no joke. The number of genes you want protein structures for is \
                              {}, which is a lot (of time!). If its taking too long, consider using the --very-fast flag. \
                              CTRL + C to cancel.".format(len(self.genes_of_interest)))

        # if self.percent_identical_cutoff is < 25, you should be careful about accuracy of models
        if self.min_proper_pident < 25:
            self.run.warning("You selected a percent identical cutoff of {}%. Below 25%, you should pay close attention \
                              to the quality of the proteins...".format(self.min_proper_pident))

        # check that DSSP exists
        if self.skip_DSSP:
            self.run.warning("You requested to skip amino acid residue annotation with DSSP. A bold move only an expert could justify... \
                              Anvi'o's respect for you increases slightly.")

        else:
            if utils.is_program_exists("mkdssp", dont_raise=True): # mkdssp is newer and preferred
                self.DSSP_executable = "mkdssp"

            if not self.DSSP_executable:
                if utils.is_program_exists("dssp", dont_raise=True):
                    self.DSSP_executable = "dssp"
                else:
                    raise ConfigError("An anvi'o function needs 'mkdssp' or 'dssp' to be installed on your system, but\
                                       neither seem to appear in your path :/ If you are certain you have either on your\
                                       system (for instance you can run either by typing 'mkdssp' or 'dssp' in your terminal\
                                       window), you may want to send a detailed bug report. If you want to skip secondary\
                                       structure and solvent accessibility annotation, provide the flag --skip-DSSP.")

            self.run.info_single("Anvi'o found the DSSP executable `%s`, and will use it."\
                                  % self.DSSP_executable, nl_before=1, nl_after=1)


    def get_genes_of_interest(self):
        """
        nabs the genes of interest based on user arguments (self.args)
        """
        self.genes_of_interest = None

        # identify the gene caller ids of all genes available
        self.genes_in_database = set(dbops.ContigsSuperclass(self.args).genes_in_splits.keys())

        if not self.genes_in_database:
            raise ConfigError("This contigs database does not contain any identified genes...")

        # settling genes of interest
        if self.genes_of_interest_path and self.gene_caller_ids:
            raise ConfigError("You can't provide a gene caller id from the command line, and a list of gene caller ids\
                               as a file at the same time, obviously.")

        if self.gene_caller_ids:
            self.gene_caller_ids = set([x.strip() for x in self.gene_caller_ids.split(',')])

            self.genes_of_interest = []
            for gene in self.gene_caller_ids:
                try:
                    self.genes_of_interest.append(int(gene))
                except:
                    raise ConfigError("Anvi'o does not like your gene caller id '%s'..." % str(gene))

            self.genes_of_interest = set(self.genes_of_interest)

        elif self.genes_of_interest_path:
            filesnpaths.is_file_tab_delimited(self.genes_of_interest_path, expected_number_of_fields=1)

            try:
                self.genes_of_interest = set([int(s.strip()) for s in open(self.genes_of_interest_path).readlines()])
            except ValueError:
                raise ConfigError("Well. Anvi'o was working on your genes of interest ... and ... those gene IDs did not\
                                   look like anvi'o gene caller ids :/ Anvi'o is now sad.")

        if not self.genes_of_interest:
            # no genes of interest are specified. Assuming all, which could be innumerable--raise warning
            self.genes_of_interest = self.genes_in_database
            self.run.warning("You did not specify any genes of interest, so anvi'o will assume all of them are of interest.")


    def process(self):
        """
        """

        # will be empty if all sources in self.annotation_sources_info have "skip": True
        residue_annotation_methods = [info["method"] for _, info in self.annotation_sources_info.items() if not info["skip"]]

        # which genes had structures and which did not. this information is added to the structure database self table
        has_structure = {True: [], False: []}

        for corresponding_gene_call in self.genes_of_interest:
            # MODELLER outputs a lot of stuff into its working directory. A temporary directory is
            # made for each instance of MODELLER (i.e. each protein), And bits and pieces of this
            # directory are used in the creation of the structure database. If self.full_modeller_output is
            # provided, these directories and their contents are moved into self.full_modeller_output.
            self.args.directory = filesnpaths.get_temp_directory_path()
            self.args.target_fasta_path = filesnpaths.get_temp_file_path()

            # Export sequence
            dbops.export_aa_sequences_from_contigs_db(self.contigs_db_path,
                                                      self.args.target_fasta_path,
                                                      set([corresponding_gene_call]),
                                                      quiet =True)

            # Model structure
            modeller_out = self.run_modeller(corresponding_gene_call)
            has_structure[modeller_out["structure_exists"]].append(str(corresponding_gene_call))

            # Annotate residues
            residue_info_dataframe = None
            if modeller_out["structure_exists"]:
                residue_info_dataframe = self.run_residue_annotation_for_gene(residue_annotation_methods,
                                                                              corresponding_gene_call,
                                                                              modeller_out["best_model_path"])

            # Append info to tables
            self.append_gene_info_to_tables(modeller_out, residue_info_dataframe)

            if self.full_modeller_output:
                self.dump_results_to_full_output()

        if not has_structure[True]:
            raise ConfigError("Well this is really sad. No structures were modelled, and therefore\
                               there is no structure database to create. Bye :'(")

        # add metadata
        self.structure_db.db.set_meta_value('genes_queried', ",".join([str(g) for g in self.genes_of_interest]))
        self.structure_db.db.set_meta_value('genes_with_structure', ",".join(has_structure[True]))
        self.structure_db.db.set_meta_value('genes_without_structure', ",".join(has_structure[False]))
        self.structure_db.db.set_meta_value('modeller_database', self.modeller.modeller_database)
        self.structure_db.db.set_meta_value('scoring_method', self.scoring_method)
        self.structure_db.db.set_meta_value('min_ppi', str(self.min_proper_pident))
        self.structure_db.db.set_meta_value('fast_optimization', str(int(self.very_fast)))
        self.structure_db.db.set_meta_value('deviation', self.deviation)

        self.structure_db.disconnect()


    def run_residue_annotation_for_gene(self, residue_annotation_methods, corresponding_gene_call, pdb_filepath):
        # res_annotation_for_gene is a dataframe that stores annotations made by all
        # annotation methods (e.g.  DSSP) for the current corresponding_gene_call. Each time an annotation
        # source is ran, its results are appended as columns to res_annotation_for_gene.
        # All annotation sources must have the index called "codon_order_in_gene" whose values are
        # anvi'o-indexed, i.e. the methionine has index 0. Each annotation source does NOT have
        # to annotate each residue in the gene.
        res_annotation_for_gene = pd.DataFrame({})
        for method in residue_annotation_methods:
            res_annotation_for_gene = pd.concat([res_annotation_for_gene, method(corresponding_gene_call, pdb_filepath)], axis=1)

        # add corresponding_gene_call and codon_order_in_gene as 0th and 1st columns
        res_annotation_for_gene.insert(0, "entry_id", list(range(res_annotation_for_gene.shape[0])))
        res_annotation_for_gene.insert(1, "corresponding_gene_call", corresponding_gene_call)
        res_annotation_for_gene.insert(2, "codon_order_in_gene", res_annotation_for_gene.index)

        return res_annotation_for_gene


    def dump_results_to_full_output(self):
        """
        if self.full_modeller_output, all files from MODELLERs temp directory are recursively moved into
        output_gene_dir. Otherwise, the list of files we care about are defined in this function
        and moved into output_gene_dir.
        """
        output_gene_dir = os.path.join(self.full_modeller_output, self.modeller.corresponding_gene_call)
        filesnpaths.check_output_directory(output_gene_dir)
        shutil.move(self.modeller.directory, output_gene_dir)


    def run_DSSP(self, corresponding_gene_call, pdb_filepath):
        """
        DSSP is ran using the API developed in Biopython. That means we don't work directly from the
        text output of DSSP, but rather a Biopython object.
        """
        # Determine the model name by loading the structure file
        p = PDBParser()
        structure = p.get_structure(corresponding_gene_call, pdb_filepath)
        model = structure[0] # pdb files can have multiple models. DSSP assumes the first.

        # run DSSP
        residue_annotation = DSSP(model, pdb_filepath, dssp = self.DSSP_executable, acc_array = "Wilke")

        if not len(residue_annotation.keys()):
            raise ConfigError("{} didn't return any meaningful output. You can try running \
                               {} -i {} -o output.txt to test that dssp is running correctly."
                               .format(self.DSSP_executable, self.DSSP_executable, pdb_filepath))

        # convert to a digestible format
        return self.convert_DSSP_output_from_biopython_to_dataframe(residue_annotation)


    def convert_DSSP_output_from_biopython_to_dataframe(self, dssp_biopython_object):
        """
        From the DSSP module in Biopython:
            ============ ==================== ================
            Tuple Index  Biopython            Anvi'o
            ============ ==================== ================
            0            DSSP index           codon_order_in_gene
            1            Amino acid           aa
            2            Secondary structure  sec_struct
            3            Relative ASA         rel_solvent_acc
            4            Phi                  phi
            5            Psi                  psi
            6            NH__>O_1_relidx      NH_O_1_index
            7            NH__>O_1_energy      NH_O_1_energy
            8            O__>NH_1_relidx      O_NH_1_index
            9            O__>NH_1_energy      O_NH_1_energy
            10           NH__>O_2_relidx      NH_O_2_index
            11           NH__>O_2_energy      NH_O_2_energy
            12           O__>NH_2_relidx      O_NH_2_index
            13           O__>NH_2_energy      O_NH_2_energy
            ============ ==================== ================

        Changes from Biopython format to anvi'o format:
            - residue index converted from 1Met to 0Met
            - aa converted to 3-letter code
            - ss type "-" is converted to coil (C)
            - relative indicies for h-bonds replaced with absolute residue indices
              (e.g. if relative index = -1 for residue 4, the absolute residue index is 3)
        """

        one_to_three = {v: k for k, v in constants.AA_to_single_letter_code.items()}
        columns = list(self.annotation_sources_info["DSSP"]["structure"].keys())

        # convert biopython object to dictionary d
        d = {}
        for key in dssp_biopython_object.keys():
            d[key] = list(dssp_biopython_object[key])
            d[key][columns.index("codon_order_in_gene")] = utils.convert_sequence_indexing(d[key][columns.index("codon_order_in_gene")], source="M1", destination="M0")
            d[key][columns.index("aa")] = one_to_three[d[key][columns.index("aa")]]

            if d[key][columns.index("sec_struct")] == "-":
                d[key][columns.index("sec_struct")] = "C"

            for hbond in ["NH_O_1", "O_NH_1", "NH_O_2", "O_NH_2"]:
                res_index = d[key][columns.index("codon_order_in_gene")]
                rel_index = d[key][columns.index(hbond+"_index")]

                if rel_index == 0:
                    d[key][columns.index(hbond+"_index")] = np.nan
                    d[key][columns.index(hbond+"_energy")] = np.nan

                else:
                    d[key][columns.index(hbond+"_index")] = res_index + rel_index

        # convert dictionary d to dataframe df
        return pd.DataFrame(d, index=columns).T.set_index("codon_order_in_gene")


    def run_modeller(self, corresponding_gene_call):
        self.modeller = MODELLER.MODELLER(self.args, run=self.run, progress=self.progress)
        modeller_out = self.modeller.process()

        return modeller_out


    def append_gene_info_to_tables(self, modeller_out, residue_info_dataframe):
        """
        Modeller and residue annotation sources have been called, now it is time to wrangle these
        data into formats that can be appended to their respective structure database tables.
        """
        corresponding_gene_call = modeller_out["corresponding_gene_call"]

        # templates is always added, even when structure was not modelled
        templates = pd.DataFrame(modeller_out["templates"])
        templates.insert(0, "corresponding_gene_call", corresponding_gene_call)
        templates = templates.reset_index().rename(columns={"index": "entry_id"})
        self.structure_db.entries[t.structure_templates_table_name] = \
            self.structure_db.entries[t.structure_templates_table_name].append(templates)
        self.structure_db.store(t.structure_templates_table_name, key="entry_id")


        # entries that are only added if a structure was modelled
        if modeller_out["structure_exists"]:

            # models
            models = pd.DataFrame(modeller_out["models"])
            models.insert(0, "corresponding_gene_call", corresponding_gene_call)
            models = models.reset_index().rename(columns={"index": "entry_id"})
            self.structure_db.entries[t.structure_models_table_name] = \
                self.structure_db.entries[t.structure_models_table_name].append(models)
            self.structure_db.store(t.structure_models_table_name, key="entry_id")

            # pdb file data
            pdb_file = open(modeller_out["best_model_path"], 'rb')
            pdb_contents = pdb_file.read()
            pdb_file.close()
            pdb_table_entry = (corresponding_gene_call, pdb_contents)
            self.structure_db.entries[t.structure_pdb_data_table_name].append(pdb_table_entry)
            self.structure_db.store(t.structure_pdb_data_table_name)

            # residue_info
            self.structure_db.entries[t.structure_residue_info_table_name] = \
                self.structure_db.entries[t.structure_residue_info_table_name].append(residue_info_dataframe)
            self.structure_db.store(t.structure_residue_info_table_name, key="entry_id")


