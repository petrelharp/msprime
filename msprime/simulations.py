#
# Copyright (C) 2015-2020 University of Oxford
#
# This file is part of msprime.
#
# msprime is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# msprime is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with msprime.  If not, see <http://www.gnu.org/licenses/>.
#
"""
Module responsible for running simulations.
"""
import bisect
import collections
import copy
import gzip
import inspect
import json
import logging
import math
import os
import sys
import warnings

import numpy as np

import _msprime
import tskit
from . import mutations
from . import provenance
from . import utils
from _msprime import NODE_IS_CA_EVENT  # NOQA
from _msprime import NODE_IS_CEN_EVENT  # NOQA
from _msprime import NODE_IS_MIG_EVENT  # NOQA
from _msprime import NODE_IS_RE_EVENT  # NOQA
from _msprime import RandomGenerator

# Make the low-level generator appear like its from this module
# NOTE: Using these classes directly from client code is undocumented
# and may be removed in future versions.

# Make sure the GSL error handler is turned off so that we can be sure that
# we don't abort on errors. This can be reset by using the function
# _msprime.restore_gsl_error_handler(), which will set the error handler to
# the value that it had before this function was called.
_msprime.unset_gsl_error_handler()


logger = logging.getLogger(__name__)


Sample = collections.namedtuple("Sample", ["population", "time"])


def model_factory(model, reference_size=1):
    """
    Returns a simulation model corresponding to the specified model and
    reference size.
    - If model is None, the default simulation model is returned with the
      specified reference size.
    - If model is a string, return the corresponding model instance
      with the specified reference size
    - If model is an instance of SimulationModel, return a copy of it.
      In this case, if the model's reference_size is None, set it to the
      parameter reference size.
    - Otherwise return a type error.
    """
    model_map = {
        "hudson": StandardCoalescent(reference_size),
        "smc": SmcApproxCoalescent(reference_size),
        "smc_prime": SmcPrimeApproxCoalescent(reference_size),
        "dtwf": DiscreteTimeWrightFisher(reference_size),
        "wf_ped": WrightFisherPedigree(reference_size),
    }
    if model is None:
        model_instance = StandardCoalescent(reference_size)
    elif isinstance(model, str):
        lower_model = model.lower()
        if lower_model not in model_map:
            raise ValueError(
                "Model '{}' unknown. Choose from {}".format(
                    model, list(model_map.keys())
                )
            )
        model_instance = model_map[lower_model]
    elif isinstance(model, SimulationModel):
        model_instance = copy.copy(model)
        if model_instance.reference_size is None:
            model_instance.reference_size = reference_size
    else:
        raise TypeError(
            "Simulation model must be a string or an instance of SimulationModel"
        )
    return model_instance


def _check_population_configurations(population_configurations):
    err = (
        "Population configurations must be a list of PopulationConfiguration instances"
    )
    for config in population_configurations:
        if not isinstance(config, PopulationConfiguration):
            raise TypeError(err)


def _replicate_generator(
    sim, mutation_generator, num_replicates, provenance_dict, end_time
):
    """
    Generator function for the many-replicates case of the simulate
    function.
    """

    encoded_provenance = None
    # The JSON is modified for each replicate to insert the replicate number.
    # To avoid repeatedly encoding the same JSON (which can take milliseconds)
    # we insert a replaceable string.
    placeholder = "@@_MSPRIME_REPLICATE_INDEX_@@"
    if provenance_dict is not None:
        provenance_dict["parameters"]["replicate_index"] = placeholder
        encoded_provenance = provenance.json_encode_provenance(
            provenance_dict, num_replicates
        )

    for j in range(num_replicates):
        sim.run(end_time)
        replicate_provenance = None
        if encoded_provenance is not None:
            replicate_provenance = encoded_provenance.replace(
                f'"{placeholder}"', str(j)
            )
        tree_sequence = sim.get_tree_sequence(mutation_generator, replicate_provenance)
        yield tree_sequence
        sim.reset()


def simulator_factory(
    sample_size=None,
    Ne=1,
    random_generator=None,
    length=None,
    recombination_rate=None,
    recombination_map=None,
    population_configurations=None,
    pedigree=None,
    migration_matrix=None,
    samples=None,
    demographic_events=[],
    model=None,
    record_migrations=False,
    from_ts=None,
    start_time=None,
    end_time=None,
    record_full_arg=False,
    num_labels=None,
    gene_conversion_rate=None,
    gene_conversion_track_length=None,
):
    """
    Convenience method to create a simulator instance using the same
    parameters as the `simulate` function. Primarily used for testing.
    """
    condition = (
        sample_size is None
        and population_configurations is None
        and samples is None
        and from_ts is None
    )
    if condition:
        raise ValueError(
            "Either sample_size, population_configurations, samples or from_ts must "
            "be specified"
        )
    the_samples = None
    if sample_size is not None:
        if samples is not None:
            raise ValueError("Cannot specify sample size and samples simultaneously.")
        if population_configurations is not None:
            raise ValueError(
                "Cannot specify sample size and population_configurations "
                "simultaneously."
            )
        s = Sample(population=0, time=0.0)
        # In pedigrees samples are diploid individuals
        if pedigree is not None:
            sample_size *= 2  # TODO: Update for different ploidy
        the_samples = [s for _ in range(sample_size)]
    # If we have population configurations we may have embedded sample_size
    # values telling us how many samples to take from each population.
    if population_configurations is not None:
        if pedigree is not None:
            raise NotImplementedError(
                "Cannot yet specify population configurations "
                "and pedigrees simultaneously"
            )
        _check_population_configurations(population_configurations)
        if samples is None:
            the_samples = []
            for j, conf in enumerate(population_configurations):
                if conf.sample_size is not None:
                    the_samples += [(j, 0) for _ in range(conf.sample_size)]
        else:
            for conf in population_configurations:
                if conf.sample_size is not None:
                    raise ValueError(
                        "Cannot specify population configuration sample size"
                        "and samples simultaneously"
                    )
            the_samples = samples
    elif samples is not None:
        the_samples = samples

    if start_time is not None and start_time < 0:
        raise ValueError("start_time cannot be negative")

    if num_labels is not None and num_labels < 1:
        raise ValueError("Must have at least one structured coalescent label")

    if from_ts is not None:
        if not isinstance(from_ts, tskit.TreeSequence):
            raise TypeError("from_ts must be a TreeSequence instance.")
        population_mismatch_message = (
            "Mismatch in the number of populations in from_ts and simulation "
            "parameters. The number of populations in the simulation must be "
            "equal to the number of populations in from_ts"
        )
        if population_configurations is None:
            if from_ts.num_populations != 1:
                raise ValueError(population_mismatch_message)
        else:
            if from_ts.num_populations != len(population_configurations):
                raise ValueError(population_mismatch_message)

    if recombination_map is None:
        # Default to 1 if no from_ts; otherwise default to the sequence length
        # of from_ts
        if from_ts is None:
            the_length = 1 if length is None else length
        else:
            the_length = from_ts.sequence_length if length is None else length
        the_rate = 0 if recombination_rate is None else recombination_rate
        if the_length <= 0:
            raise ValueError("Cannot provide non-positive sequence length")
        if the_rate < 0:
            raise ValueError("Cannot provide negative recombination rate")
        recomb_map = RecombinationMap.uniform_map(the_length, the_rate, discrete=False)
    else:
        if length is not None or recombination_rate is not None:
            raise ValueError(
                "Cannot specify length/recombination_rate along with "
                "a recombination map"
            )
        recomb_map = recombination_map

    # FIXME check the valid inputs for GC. Should we allow it when we
    # have a non-trivial genetic map?
    if gene_conversion_rate is None:
        gene_conversion_rate = 0
    else:
        if not recomb_map.discrete:
            raise ValueError(
                "Cannot specify gene_conversion_rate along with "
                "a nondiscrete recombination map"
            )
    if gene_conversion_track_length is None:
        gene_conversion_track_length = 1

    if from_ts is not None:
        if from_ts.sequence_length != recomb_map.get_length():
            raise ValueError(
                "The simulated sequence length must be the same as "
                "from_ts.sequence_length"
            )

    sim = Simulator(the_samples, recomb_map, model, Ne, from_ts)
    sim.store_migrations = record_migrations
    sim.store_full_arg = record_full_arg
    sim.start_time = start_time
    sim.num_labels = num_labels
    sim.gene_conversion_rate = gene_conversion_rate
    sim.gene_conversion_track_length = gene_conversion_track_length
    rng = random_generator
    if rng is None:
        rng = RandomGenerator(utils.get_random_seed())
    sim.random_generator = rng
    if population_configurations is not None:
        sim.set_population_configurations(population_configurations)
    if migration_matrix is not None:
        sim.set_migration_matrix(migration_matrix)
    if demographic_events is not None:
        sim.set_demographic_events(demographic_events)
    if pedigree is not None:
        if not isinstance(sim.model, WrightFisherPedigree):
            raise NotImplementedError("Pedigree can only be specified for wf_ped model")
        sim.set_pedigree(pedigree)
    return sim


def simulate(
    sample_size=None,
    Ne=1,
    length=None,
    recombination_rate=None,
    recombination_map=None,
    mutation_rate=None,
    population_configurations=None,
    pedigree=None,
    migration_matrix=None,
    demographic_events=[],
    samples=None,
    model=None,
    record_migrations=False,
    random_seed=None,
    mutation_generator=None,
    num_replicates=None,
    replicate_index=None,
    from_ts=None,
    start_time=None,
    end_time=None,
    record_full_arg=False,
    num_labels=None,
    record_provenance=True,
    # FIXME add documentation for these.
    gene_conversion_rate=None,
    gene_conversion_track_length=None,
):
    """
    Simulates the coalescent with recombination under the specified model
    parameters and returns the resulting :class:`tskit.TreeSequence`. Note that
    Ne is the effective diploid population size (so the effective number
    of genomes in the population is 2*Ne), but ``sample_size`` is the
    number of (monoploid) genomes sampled.

    :param int sample_size: The number of sampled monoploid genomes.  If not
        specified or None, this defaults to the sum of the subpopulation sample
        sizes. Either ``sample_size``, ``population_configurations`` or
        ``samples`` must be specified.
    :param float Ne: The effective (diploid) population size for the reference
        population. This defaults to 1 if not specified.
        Please see the :ref:`sec_api_simulation_models` section for more details
        on specifying simulations models.
    :param float length: The length of the simulated region in bases.
        This parameter cannot be used along with ``recombination_map``.
        Defaults to 1 if not specified.
    :param float recombination_rate: The rate of recombination per base
        per generation. This parameter cannot be used along with
        ``recombination_map``. Defaults to 0 if not specified.
    :param recombination_map: The map
        describing the changing rates of recombination along the simulated
        chromosome. This parameter cannot be used along with the
        ``recombination_rate`` or ``length`` parameters, as these
        values are encoded within the map. Defaults to a uniform rate as
        described in the ``recombination_rate`` parameter if not specified.
    :type recombination_map: :class:`.RecombinationMap`
    :param float mutation_rate: The rate of infinite sites
        mutations per unit of sequence length per generation.
        If not specified, no mutations are generated. This option only
        allows for infinite sites mutations with a binary (i.e., 0/1)
        alphabet. For more control over the mutational process, please
        use the :func:`.mutate` function.
    :param list population_configurations: The list of
        :class:`.PopulationConfiguration` instances describing the
        sampling configuration, relative sizes and growth rates of
        the populations to be simulated. If this is not specified,
        a single population with a sample of size ``sample_size``
        is assumed.
    :type population_configurations: list or None.
    :param list migration_matrix: The matrix describing the rates
        of migration between all pairs of populations. If :math:`N`
        populations are defined in the ``population_configurations``
        parameter, then the migration matrix must be an
        :math:`N \\times N` matrix with 0 on the diagonal, consisting of
        :math:`N` lists of length :math:`N` or an :math:`N \\times N` numpy
        array, with the [j, k]th element giving the fraction of
        population j that consists of migrants from population k in each
        generation.
    :param list demographic_events: The list of demographic events to
        simulate. Demographic events describe changes to the populations
        in the past. Events should be supplied in non-decreasing
        order of time in the past. Events with the same time value will be
        applied sequentially in the order that they were supplied before the
        simulation algorithm continues with the next time step.
    :param list samples: The list specifying the location and time of
        all samples. This parameter may be used to specify historical
        samples, and cannot be used in conjunction with the ``sample_size``
        parameter. Each sample is a (``population``, ``time``) pair
        such that the sample in position ``j`` in the list of samples
        is drawn in the specified population at the specfied time. Time
        is measured in generations ago, as elsewhere.
    :param int random_seed: The random seed. If this is `None`, a
        random seed will be automatically generated. Valid random
        seeds must be between 1 and :math:`2^{32} - 1`.
    :param int num_replicates: The number of replicates of the specified
        parameters to simulate. If this is not specified or None,
        no replication is performed and a :class:`tskit.TreeSequence` object
        returned. If :obj:`num_replicates` is provided, the specified
        number of replicates is performed, and an iterator over the
        resulting :class:`tskit.TreeSequence` objects returned.
    :param int replicate_index: Return only a specific tree
        sequence from the set of replicates. This is used to recreate a specific tree
        sequence from e.g. provenance. This argument only makes sense when used with
        `random seed`, and is not compatible with `num_replicates`. Note also that
        msprime will have to create and discard all the tree sequences up to this index.
    :param tskit.TreeSequence from_ts: If specified, initialise the simulation
        from the root segments of this tree sequence and return the
        completed tree sequence. Please see :ref:`here
        <sec_api_simulate_from>` for details on the required properties
        of this tree sequence and its interactions with other parameters.
        (Default: None).
    :param float start_time: If specified, set the initial time that the
        simulation starts to this value. If not specified, the start
        time is zero if performing a simulation of a set of samples,
        or is the time of the oldest node if simulating from an
        existing tree sequence (see the ``from_ts`` parameter).
    :param float end_time: If specified, terminate the simulation at the
        specified time. In the returned tree sequence, all rootward paths from
        samples with time < end_time will end in a node with one child with
        time equal to end_time. Sample nodes with time >= end_time will
        also be present in the output tree sequence. If not specified or ``None``,
        run the simulation until all samples have an MRCA at all positions in
        the genome.
    :param bool record_full_arg: If True, record all intermediate nodes
        arising from common ancestor and recombination events in the output
        tree sequence. This will result in unary nodes (i.e., nodes in marginal
        trees that have only one child). Defaults to False.
    :param model: The simulation model to use.
        This can either be a string (e.g., ``"smc_prime"``) or an instance of
        a simulation model class (e.g, ``msprime.DiscreteTimeWrightFisher(100)``.
        Please see the :ref:`sec_api_simulation_models` section for more details
        on specifying simulations models.
    :type model: str or simulation model instance
    :param bool record_provenance: If True, record all configuration and parameters
        required to recreate the tree sequence. These can be accessed
        via ``TreeSequence.provenances()``).
    :return: The :class:`tskit.TreeSequence` object representing the results
        of the simulation if no replication is performed, or an
        iterator over the independent replicates simulated if the
        :obj:`num_replicates` parameter has been used.
    :rtype: :class:`tskit.TreeSequence` or an iterator over
        :class:`tskit.TreeSequence` replicates.
    :warning: If using replication, do not store the results of the
        iterator in a list! For performance reasons, the same
        underlying object may be used for every TreeSequence
        returned which will most likely lead to unexpected behaviour.
    """

    seed = random_seed
    if random_seed is None:
        seed = utils.get_random_seed()
    seed = int(seed)
    rng = RandomGenerator(seed)

    provenance_dict = None
    if record_provenance:
        argspec = inspect.getargvalues(inspect.currentframe())
        # num_replicates is excluded as provenance is per replicate
        # replicate index is excluded as it is inserted for each replicate
        parameters = {
            "command": "simulate",
            **{
                arg: argspec.locals[arg]
                for arg in argspec.args
                if arg not in ["num_replicates", "replicate_index"]
            },
        }
        parameters["random_seed"] = seed
        provenance_dict = provenance.get_provenance_dict(parameters)

    sim = simulator_factory(
        sample_size=sample_size,
        random_generator=rng,
        Ne=Ne,
        length=length,
        recombination_rate=recombination_rate,
        recombination_map=recombination_map,
        population_configurations=population_configurations,
        pedigree=pedigree,
        migration_matrix=migration_matrix,
        demographic_events=demographic_events,
        samples=samples,
        model=model,
        record_migrations=record_migrations,
        from_ts=from_ts,
        start_time=start_time,
        record_full_arg=record_full_arg,
        num_labels=num_labels,
        gene_conversion_rate=gene_conversion_rate,
        gene_conversion_track_length=gene_conversion_track_length,
    )

    if mutation_generator is not None:
        # This error was added in version 0.6.1.
        raise ValueError(
            "mutation_generator is not longer supported. Please use "
            "msprime.mutate instead"
        )

    if mutation_rate is not None:
        # There is ambiguity in how we should throw mutations onto partially
        # built tree sequences: on the whole thing, or must the newly added
        # topology? Before or after start_time? We avoid this complexity by
        # asking the user to use mutate(), which should have the required
        # flexibility.
        if from_ts is not None:
            raise ValueError(
                "Cannot specify mutation rate combined with from_ts. Please use "
                "msprime.mutate on the final tree sequence instead"
            )
        # There is ambiguity in how the start_time argument should interact with
        # the mutation generator: should we throw mutations down on the whole
        # tree or just the (partial) edges after start_time? To avoid complicating
        # things here, make the user use mutate() which should have the flexibility
        # to do whatever is needed.
        if start_time is not None and start_time > 0:
            raise ValueError(
                "Cannot specify mutation rate combined with a non-zero "
                "start_time. Please use msprime.mutate on the returned "
                "tree sequence instead"
            )
    # TODO when the ``discrete`` parameter is added here, pass it through
    # to make it a property of the mutation generator.
    mutation_generator = mutations._simple_mutation_generator(
        mutation_rate, sim.sequence_length, sim.random_generator
    )
    if replicate_index is not None and random_seed is None:
        raise ValueError(
            "Cannot specify replicate_index without random_seed as this "
            "has the same effect as not specifying replicate_index i.e. a "
            "random tree sequence"
        )
    if replicate_index is not None and num_replicates is not None:
        raise ValueError(
            "Cannot specify replicate_index with num_replicates as only "
            "the replicate_index specified will be returned."
        )
    if num_replicates is None and replicate_index is None:
        replicate_index = 0
    if replicate_index is not None:
        iterator = _replicate_generator(
            sim, mutation_generator, replicate_index + 1, provenance_dict, end_time
        )
        # Return the last element of the iterator
        ts = next(iterator)
        for ts in iterator:
            continue
        return ts
    else:
        return _replicate_generator(
            sim, mutation_generator, num_replicates, provenance_dict, end_time
        )


class Simulator:
    """
    Class to simulate trees under a variety of population models.
    """

    def __init__(
        self, samples, recombination_map, model="hudson", Ne=0.25, from_ts=None
    ):
        if from_ts is None:
            if len(samples) < 2:
                raise ValueError("Sample size must be >= 2")
            self.samples = samples
        else:
            if samples is not None and len(samples) > 0:
                raise ValueError("Cannot specify samples with from_ts")
            self.samples = []
        if not isinstance(recombination_map, RecombinationMap):
            raise TypeError("RecombinationMap instance required")
        self.ll_sim = None
        self.model = model_factory(model, Ne)
        self.recombination_map = recombination_map
        self.from_ts = from_ts
        self.start_time = None
        self.random_generator = None
        self.population_configurations = [
            PopulationConfiguration(initial_size=self.model.reference_size)
        ]
        self.pedigree = None
        self.migration_matrix = [[0]]
        self.demographic_events = []
        self.model_change_events = []
        self.store_migrations = False
        self.store_full_arg = False
        self.num_labels = None
        # We always need at least n segments, so no point in making
        # allocation any smaller than this.
        num_samples = (
            len(self.samples) if self.samples is not None else from_ts.num_samples
        )
        block_size = 64 * 1024
        self.segment_block_size = max(block_size, num_samples)
        self.avl_node_block_size = block_size
        self.node_mapping_block_size = block_size
        self.end_time = None
        self.gene_conversion_rate = 0
        self.gene_conversion_track_length = 1

    @property
    def sequence_length(self):
        return self.recombination_map.get_sequence_length()

    @property
    def sample_configuration(self):
        return [conf.sample_size for conf in self.population_configurations]

    @property
    def num_breakpoints(self):
        return self.ll_sim.get_num_breakpoints()

    @property
    def breakpoints(self):
        """
        Returns the recombination breakpoints translated into physical
        coordinates.
        """
        return self.ll_sim.get_breakpoints()

    @property
    def time(self):
        return self.ll_sim.get_time()

    @property
    def num_avl_node_blocks(self):
        return self.ll_sim.get_num_avl_node_blocks()

    @property
    def num_node_mapping_blocks(self):
        return self.ll_sim.get_num_node_mapping_blocks()

    @property
    def num_segment_blocks(self):
        return self.ll_sim.get_num_segment_blocks()

    @property
    def num_common_ancestor_events(self):
        return self.ll_sim.get_num_common_ancestor_events()

    @property
    def num_rejected_common_ancestor_events(self):
        return self.ll_sim.get_num_rejected_common_ancestor_events()

    @property
    def num_recombination_events(self):
        return self.ll_sim.get_num_recombination_events()

    @property
    def num_gene_conversion_events(self):
        return self.ll_sim.get_num_gene_conversion_events()

    @property
    def num_populations(self):
        return len(self.population_configurations)

    @property
    def num_migration_events(self):
        N = self.num_populations
        matrix = [[0 for j in range(N)] for k in range(N)]
        flat = self.ll_sim.get_num_migration_events()
        for j in range(N):
            for k in range(N):
                matrix[j][k] = flat[j * N + k]
        return matrix

    @property
    def total_num_migration_events(self):
        return sum(self.ll_sim.get_num_migration_events())

    @property
    def num_multiple_recombination_events(self):
        return self.ll_sim.get_num_multiple_recombination_events()

    def set_migration_matrix(self, migration_matrix):
        err = (
            "migration matrix must be a N x N square matrix encoded "
            "as a list-of-lists, where N is the number of populations "
            "defined in the population_configurations. The diagonal "
            "elements of this matrix must be zero. For example, a "
            "valid matrix for a 3 population system is "
            "[[0, 1, 1], [1, 0, 1], [1, 1, 0]]"
        )
        N = len(self.population_configurations)
        if not isinstance(migration_matrix, list):
            try:
                migration_matrix = [list(row) for row in migration_matrix]
            except TypeError:
                raise TypeError(err)
        if len(migration_matrix) != N:
            raise ValueError(err)
        for row in migration_matrix:
            if not isinstance(row, list):
                raise TypeError(err)
            if len(row) != N:
                raise ValueError(err)
        self.migration_matrix = migration_matrix

    def set_population_configurations(self, population_configurations):
        _check_population_configurations(population_configurations)
        self.population_configurations = population_configurations
        # For any populations configurations in which the initial size is None,
        # set it to the population size.
        for pop_conf in self.population_configurations:
            if pop_conf.initial_size is None:
                pop_conf.initial_size = self.model.reference_size
        # Now set the default migration matrix.
        N = len(self.population_configurations)
        self.migration_matrix = [[0 for j in range(N)] for k in range(N)]

    def set_pedigree(self, pedigree):
        if len(self.samples) % 2 != 0:
            raise ValueError(
                "In (diploid) pedigrees, must specify two " "lineages per individual."
            )

        if pedigree.is_sample is None:
            pedigree.set_samples(num_samples=len(self.samples) // 2)

        if sum(pedigree.is_sample) * 2 != len(self.samples):
            raise ValueError(
                "{} sample lineages to be simulated, but {} in pedigree".format(
                    len(self.samples), pedigree.num_samples * 2
                )
            )

        if not isinstance(self.model, WrightFisherPedigree):
            raise ValueError(
                "Can only specify pedigrees for the "
                "`msprime.WrightFisherPedigree` simulation model."
            )

        pedigree_max_time = np.max(pedigree.time)
        if len(self.demographic_events) > 0:
            de_min_time = min([x.time for x in self.demographic_events])
            if de_min_time <= pedigree_max_time:
                raise NotImplementedError(
                    "Demographic events must be older than oldest pedigree founder."
                )
        if len(self.model_change_events) > 0:
            mc_min_time = min([x.time for x in self.model_change_events])
            if mc_min_time < pedigree_max_time:
                raise NotImplementedError(
                    "Model change events earlier than founders of pedigree unsupported."
                )

        self.pedigree = pedigree.get_ll_representation()

    def set_demographic_events(self, demographic_events):
        err = (
            "Demographic events must be a list of DemographicEvent instances "
            "sorted in non-decreasing order of time."
        )
        self.demographic_events = []
        self.model_change_events = []
        for event in demographic_events:
            if not isinstance(event, DemographicEvent):
                raise TypeError(err)
            if isinstance(event, SimulationModelChange):
                # Take a copy so that we're not modifying our input params
                event = copy.copy(event)
                # Update the model so that we can parse strings and set
                # the reference size appropriately.
                event.model = model_factory(event.model, self.model.reference_size)
                self.model_change_events.append(event)
            else:
                self.demographic_events.append(event)

    def __choose_num_labels(self):
        """
        Choose the number of labels appropriately, given the simulation
        models that will be simulated.
        """
        self.num_labels = 1
        models = [self.model] + [event.model for event in self.model_change_events]
        for model in models:
            if isinstance(model, SweepGenicSelection):
                self.num_labels = 2

    def create_ll_instance(self):
        if self.num_labels is None:
            self.__choose_num_labels()
        # Now, convert the high-level values into their low-level
        # counterparts.
        ll_simulation_model = self.model.get_ll_representation()
        logger.debug("Setting initial model %s", ll_simulation_model)
        d = len(self.population_configurations)
        # The migration matrix must be flattened.
        ll_migration_matrix = [0 for j in range(d ** 2)]
        for j in range(d):
            for k in range(d):
                ll_migration_matrix[j * d + k] = self.migration_matrix[j][k]
        ll_population_configuration = [
            conf.get_ll_representation() for conf in self.population_configurations
        ]
        ll_demographic_events = [
            event.get_ll_representation(d) for event in self.demographic_events
        ]
        ll_recomb_map = self.recombination_map.get_ll_recombination_map()
        self.ll_tables = _msprime.LightweightTableCollection(
            self.recombination_map.get_sequence_length()
        )
        if self.from_ts is not None:
            from_ts_tables = self.from_ts.tables.asdict()
            self.ll_tables.fromdict(from_ts_tables)
        start_time = -1 if self.start_time is None else self.start_time
        ll_sim = _msprime.Simulator(
            samples=self.samples,
            recombination_map=ll_recomb_map,
            tables=self.ll_tables,
            start_time=start_time,
            random_generator=self.random_generator,
            model=ll_simulation_model,
            migration_matrix=ll_migration_matrix,
            population_configuration=ll_population_configuration,
            pedigree=self.pedigree,
            demographic_events=ll_demographic_events,
            store_migrations=self.store_migrations,
            store_full_arg=self.store_full_arg,
            num_labels=self.num_labels,
            segment_block_size=self.segment_block_size,
            avl_node_block_size=self.avl_node_block_size,
            node_mapping_block_size=self.node_mapping_block_size,
            gene_conversion_rate=self.gene_conversion_rate,
            gene_conversion_track_length=self.gene_conversion_track_length,
        )
        return ll_sim

    def run(self, end_time=None):
        """
        Runs the simulation until complete coalescence has occurred.
        """
        if self.ll_sim is None:
            self.ll_sim = self.create_ll_instance()
        for event in self.model_change_events:
            # If the event time is a callable, we compute the end_time
            # as a function of the current simulation time.
            current_time = self.ll_sim.get_time()
            model_start_time = event.time
            if callable(event.time):
                model_start_time = event.time(current_time)
            # If model_start_time is None, we run until the current
            # model completes. Note that when event.time is a callable
            # it can also return None for this behaviour.
            if model_start_time is None:
                model_start_time = np.inf
            if model_start_time < current_time:
                raise ValueError(
                    "Model start times out of order or not computed correctly. "
                    f"current time = {current_time}; start_time = {model_start_time}"
                )
            logger.debug("Running simulation until maximum: %f", model_start_time)
            self.ll_sim.run(model_start_time)
            ll_new_model = event.model.get_ll_representation()
            logger.debug("Changing to model %s", ll_new_model)
            self.ll_sim.set_model(ll_new_model)
        end_time = np.inf if end_time is None else end_time
        logger.debug("Running simulation until maximum: %f", end_time)
        self.ll_sim.run(end_time)
        self.ll_sim.finalise_tables()

    def get_tree_sequence(self, mutation_generator=None, provenance_record=None):
        """
        Returns a TreeSequence representing the state of the simulation.
        """
        if mutation_generator is not None:
            mutation_generator.generate(self.ll_tables)
        tables = tskit.TableCollection.fromdict(self.ll_tables.asdict())
        if provenance_record is not None:
            tables.provenances.add_row(provenance_record)
        if self.from_ts is None:
            # Add the populations with metadata
            assert len(tables.populations) == len(self.population_configurations)
            tables.populations.clear()
            for pop_config in self.population_configurations:
                tables.populations.add_row(metadata=pop_config.encoded_metadata)
        return tables.tree_sequence()

    def reset(self):
        """
        Resets the simulation so that we can perform another replicate.
        """
        if self.ll_sim is not None:
            self.ll_sim.reset()


class RecombinationMap:
    """
    A RecombinationMap represents the changing rates of recombination
    along a chromosome. This is defined via two lists of numbers:
    ``positions`` and ``rates``, which must be of the same length.
    Given an index j in these lists, the rate of recombination
    per base per generation is ``rates[j]`` over the interval
    ``positions[j]`` to ``positions[j + 1]``. Consequently, the first
    position must be zero, and by convention the last rate value
    is also required to be zero (although it is not used).

    .. warning::
        The ``num_loci`` parameter is deprecated. To set a discrete number of
        possible recombination sites along the sequence, scale ``positions``
        to the desired number of sites and set ``discrete=True`` to ensure
        recombination occurs only at integer values.

    :param list positions: The positions (in bases) denoting the
        distinct intervals where recombination rates change. These can
        be floating point values.
    :param list rates: The list of rates corresponding to the supplied
        ``positions``. Recombination rates are specified per base,
        per generation.
    :param int num_loci: **This parameter is deprecated**.
        The maximum number of non-recombining loci
        in the underlying simulation. By default this is set to
        the largest possible value, allowing the maximum resolution
        in the recombination process. However, for a finite sites
        model this can be set to smaller values.
    :param bool discrete: Whether recombination can occur only at integer
        positions. When ``False``, recombination sites can take continuous
        values. To simulate a fixed number of loci, set this parameter to
        ``True`` and scale ``positions`` to span the desired number of loci.
    """

    def __init__(self, positions, rates, num_loci=None, discrete=False, map_start=0):
        if num_loci is not None:
            if num_loci == positions[-1]:
                warnings.warn("num_loci is no longer supported and should not be used.")
            else:
                raise ValueError(
                    "num_loci does not match sequence length. "
                    "To set a discrete number of recombination sites, "
                    "scale positions to span the desired number of loci "
                    "and set discrete=True"
                )
        self._ll_recombination_map = _msprime.RecombinationMap(
            positions, rates, discrete
        )
        self.map_start = map_start

    @classmethod
    def uniform_map(cls, length, rate, num_loci=None, discrete=False):
        """
        Returns a :class:`.RecombinationMap` instance in which the recombination
        rate is constant over a chromosome of the specified length. The optional
        ``discrete`` controls whether recombination sites can occur only on integer
        positions or can take continuous values. The legacy ``num_loci`` option is
        no longer supported and should not be used.

        The following map can be used to simulate a true finite locus model
        with a fixed number of loci ``m``::

            >>> recomb_map = RecombinationMap.uniform_map(m, rate, discrete=True)

        :param float length: The length of the chromosome.
        :param float rate: The rate of recombination per unit of sequence length
            along this chromosome.
        :param int num_loci: This parameter is no longer supported.
        :param bool discrete: Whether recombination can occur only at integer
            positions. When ``False``, recombination sites can take continuous
            values. To simulate a fixed number of loci, set this parameter to
            ``True`` and set ``length`` to the desired number of loci.
        """
        return cls([0, length], [rate, 0], num_loci=num_loci, discrete=discrete)

    @classmethod
    def read_hapmap(cls, filename):
        """
        Parses the specified file in HapMap format. These files must be
        white-space-delimited, and contain a single header line (which is
        ignored), and then each subsequent line contains the starting position
        and recombination rate for the segment from that position (inclusive)
        to the starting position on the next line (exclusive). Starting
        positions of each segment are given in units of bases, and
        recombination rates in centimorgans/Megabase. The first column in this
        file is ignored, as are additional columns after the third (Position is
        assumed to be the second column, and Rate is assumed to be the third).
        If the first starting position is not equal to zero, then a
        zero-recombination region is inserted at the start of the chromosome.

        A sample of this format is as follows::

            Chromosome	Position(bp)	Rate(cM/Mb)	Map(cM)
            chr1	55550	        2.981822	0.000000
            chr1	82571	        2.082414	0.080572
            chr1	88169	        2.081358	0.092229
            chr1	254996	        3.354927	0.439456
            chr1	564598	        2.887498	1.478148
            ...
            chr1	182973428	2.512769	122.832331
            chr1	183630013	0.000000	124.482178

        :param str filename: The name of the file to be parsed. This may be
            in plain text or gzipped plain text.
        """
        positions = []
        rates = []
        if filename.endswith(".gz"):
            f = gzip.open(filename)
        else:
            f = open(filename)
        try:
            # Skip the header line
            f.readline()
            for j, line in enumerate(f):
                pos, rate, = map(float, line.split()[1:3])
                if j == 0:
                    map_start = pos
                    if pos != 0:
                        positions.append(0)
                        rates.append(0)
                positions.append(pos)
                # Rate is expressed in centimorgans per megabase, which
                # we convert to per-base rates
                rates.append(rate * 1e-8)
            if rate != 0:
                raise ValueError(
                    "The last rate provided in the recombination map must be zero"
                )
        finally:
            f.close()
        return cls(positions, rates, map_start=map_start)

    @property
    def mean_recombination_rate(self):
        """
        Return the weighted mean recombination rate
        across all windows of the entire recombination map.
        """
        chrom_length = self._ll_recombination_map.get_sequence_length()

        positions = self._ll_recombination_map.get_positions()
        positions_diff = self._ll_recombination_map.get_positions()[1:]
        positions_diff = np.append(positions_diff, chrom_length)
        window_sizes = positions_diff - positions

        weights = window_sizes / chrom_length
        if self.map_start != 0:
            weights[0] = 0
        rates = self._ll_recombination_map.get_rates()

        return np.average(rates, weights=weights)

    def slice(self, start=None, end=None, trim=False):
        """
        Returns a subset of this recombination map between the specified end
        points. If start is None, it defaults to 0. If end is None, it defaults
        to the end of the map. If trim is True, remove the flanking
        zero recombination rate regions such that the sequence length of the
        new recombination map is end - start.
        """
        positions = self.get_positions()
        rates = self.get_rates()

        if start is None:
            i = 0
            start = 0
        if end is None:
            end = positions[-1]
            j = len(positions)

        if (
            start < 0
            or end < 0
            or start > positions[-1]
            or end > positions[-1]
            or start > end
        ):
            raise IndexError(f"Invalid subset: start={start}, end={end}")

        if start != 0:
            i = bisect.bisect_left(positions, start)
            if start < positions[i]:
                i -= 1
        if end != positions[-1]:
            j = bisect.bisect_right(positions, end, lo=i)

        new_positions = list(positions[i:j])
        new_rates = list(rates[i:j])
        new_positions[0] = start
        if end > new_positions[-1]:
            new_positions.append(end)
            new_rates.append(0)
        else:
            new_rates[-1] = 0
        if trim:
            new_positions = [pos - start for pos in new_positions]
        else:
            if new_positions[0] != 0:
                if new_rates[0] == 0:
                    new_positions[0] = 0
                else:
                    new_positions.insert(0, 0)
                    new_rates.insert(0, 0.0)
            if new_positions[-1] != positions[-1]:
                new_positions.append(positions[-1])
                new_rates.append(0)
        return self.__class__(new_positions, new_rates, discrete=self.discrete)

    def __getitem__(self, key):
        """
        Use slice syntax for obtaining a recombination map subset. E.g.
            >>> recomb_map_4m_to_5m = recomb_map[4e6:5e6]
        """
        if not isinstance(key, slice) or key.step is not None:
            raise TypeError("Only interval slicing is supported")
        start, end = key.start, key.stop
        if start is not None and start < 0:
            start += self.get_sequence_length()
        if end is not None and end < 0:
            end += self.get_sequence_length()
        return self.slice(start=start, end=end, trim=True)

    def get_ll_recombination_map(self):
        return self._ll_recombination_map

    def physical_to_genetic(self, physical_x):
        return self._ll_recombination_map.position_to_mass(physical_x)

    def physical_to_discrete_genetic(self, physical_x):
        raise ValueError("Discrete genetic space is no longer supported")

    def genetic_to_physical(self, genetic_x):
        return self._ll_recombination_map.mass_to_position(genetic_x)

    def get_total_recombination_rate(self):
        return self._ll_recombination_map.get_total_recombination_rate()

    def get_per_locus_recombination_rate(self):
        raise ValueError("Genetic loci are no longer supported")

    def get_size(self):
        return self._ll_recombination_map.get_size()

    def get_num_loci(self):
        raise ValueError("num_loci is no longer supported")

    def get_positions(self):
        # For compatability with existing code we convert to a list
        return list(self._ll_recombination_map.get_positions())

    def get_rates(self):
        # For compatability with existing code we convert to a list
        return list(self._ll_recombination_map.get_rates())

    def get_sequence_length(self):
        return self._ll_recombination_map.get_sequence_length()

    def get_length(self):
        # Deprecated: use sequence_length instead
        return self.get_sequence_length()

    @property
    def discrete(self):
        return self._ll_recombination_map.get_discrete()

    def asdict(self):
        return {
            "positions": self.get_positions(),
            "rates": self.get_rates(),
            "discrete": self.discrete,
            "map_start": self.map_start,
        }


class PopulationConfiguration:
    """
    The initial configuration of a population (or deme) in a simulation.

    :param int sample_size: The number of initial samples that are drawn
        from this population.
    :param float initial_size: The absolute size of the population at time
        zero. Defaults to the reference population size :math:`N_e`.
    :param float growth_rate: The forwards-time exponential growth rate of the
        population per generation. Growth rates can be negative. This is zero for a
        constant population size, and positive for a population that has been
        growing. Defaults to 0.
    :param dict metadata: A JSON-encodable dictionary of metadata to associate
        with the corresponding Population in the output tree sequence.
        If not specified or None, no metadata is stored (i.e., an empty bytes array).
        Note that this metadata is ignored when using the ``from_ts`` argument to
        :func:`simulate`, as the population definitions in the tree sequence that
        is used as the starting point take precedence.
    """

    def __init__(
        self, sample_size=None, initial_size=None, growth_rate=0.0, metadata=None
    ):
        if initial_size is not None and initial_size <= 0:
            raise ValueError("Population size must be > 0")
        if sample_size is not None and sample_size < 0:
            raise ValueError("Sample size must be >= 0")
        self.sample_size = sample_size
        self.initial_size = initial_size
        self.growth_rate = growth_rate
        self.metadata = metadata
        self.encoded_metadata = b""
        if self.metadata is not None:
            self.encoded_metadata = json.dumps(self.metadata).encode()

    def get_ll_representation(self):
        """
        Returns the low-level representation of this PopulationConfiguration.
        """
        return {"initial_size": self.initial_size, "growth_rate": self.growth_rate}

    def asdict(self):
        """
        Returns a dict of arguments to recreate this PopulationConfiguration
        """
        ret = dict(self.__dict__)
        del ret["encoded_metadata"]
        return ret


class Pedigree:
    """
    Class representing a pedigree for simulations.

    :param ndarray individual: A 1D integer array containing strictly positive unique IDs
        for each individual in the pedigree
    :param ndarray parents: A 2D integer array containing the indices, in the
        individual array, of an individual's parents
    :param ndarray time: A 1D float array containing the time of each
        individual, in generations in the past
    :param int ploidy: The ploidy of individuals in the pedigree. Currently
        only ploidy of 2 is supported
    """

    def __init__(self, individual, parents, time, is_sample=None, sex=None, ploidy=2):
        if ploidy != 2:
            raise NotImplementedError("Ploidy != 2 not currently supported")

        if sex is not None:
            raise NotImplementedError(
                "Assigning individual sexes not currently supported"
            )

        if parents.shape[1] != ploidy:
            raise ValueError(
                "Ploidy {} conflicts with number of parents {}".format(
                    ploidy, parents.shape[1]
                )
            )

        if np.min(individual) <= 0:
            raise ValueError("Individual IDs must be > 0")

        self.individual = individual.astype(np.int32)
        self.num_individuals = len(individual)
        self.parents = parents.astype(np.int32)
        self.time = time.astype(np.float64)
        self.sex = sex
        self.ploidy = int(ploidy)

        self.is_sample = None
        if is_sample is not None:
            self.is_sample = is_sample.astype(np.uint32)
            self.samples = np.array(is_sample)[np.where(is_sample == 1)]
            self.num_samples = len(self.samples)

    def set_samples(self, num_samples=None, sample_IDs=None, probands_only=True):
        if probands_only is not True:
            raise NotImplementedError("Only probands may currently be set as samples.")

        if num_samples is None and sample_IDs is None:
            raise ValueError("Must specify one of num_samples of sample_IDs")

        if num_samples is not None and sample_IDs is not None:
            raise ValueError("Cannot specify both samples and num_samples.")

        self.is_sample = np.zeros((self.num_individuals), dtype=np.uint32)

        all_indices = range(len(self.individual))
        proband_indices = set(all_indices).difference(self.parents.ravel())

        if num_samples is not None:
            self.num_samples = num_samples
            if self.num_samples > len(proband_indices):
                raise ValueError(
                    (
                        "Cannot specify more samples ({}) than there are "
                        "probands in the pedigree ({}) "
                    ).format(self.num_samples, len(proband_indices))
                )
            sample_indices = np.random.choice(
                list(proband_indices), size=self.num_samples, replace=False
            )

        elif sample_IDs is not None:
            self.num_samples = len(sample_IDs)

            indices = all_indices
            if probands_only:
                indices = proband_indices

            sample_set = set(sample_IDs)
            sample_indices = [i for i in indices if self.individual[i] in sample_set]

        if len(sample_indices) != self.num_samples:
            raise ValueError(
                "Sample size mismatch - duplicate sample IDs or sample ID not "
                "in pedigree"
            )

        self.is_sample[sample_indices] = 1

    def get_proband_indices(self):
        all_indices = range(len(self.individual))
        proband_indices = set(all_indices).difference(self.parents.ravel())

        return sorted(proband_indices)

    def get_ll_representation(self):
        """
        Returns the low-level representation of this Pedigree.
        """
        return {
            "individual": self.individual,
            "parents": self.parents,
            "time": self.time,
            "is_sample": self.is_sample,
        }

    @staticmethod
    def get_times(individual, parent_IDs=None, parents=None, check=False):
        """
        For pedigrees without specified times, crudely assigns times to
        all individuals.
        """
        if parents is None and parent_IDs is None:
            raise ValueError("Must specify either parent IDs or parent indices")

        if parents is None:
            parents = Pedigree.parent_ID_to_index(individual, parent_IDs)

        time = np.zeros(len(individual))
        all_indices = range(len(individual))
        proband_indices = set(all_indices).difference(parents.ravel())
        climber_indices = proband_indices

        t = 0
        while len(climber_indices) > 0:
            next_climbers = []
            for c_idx in climber_indices:
                if time[c_idx] < t:
                    time[c_idx] = t

                next_parents = [p for p in parents[c_idx] if p >= 0]
                next_climbers.extend(next_parents)

            climber_indices = list(set(next_climbers))
            t += 1

        if check:
            Pedigree.check_times(individual, parents, time)

        return time

    @staticmethod
    def check_times(individual, parents, time):
        for i, ind in enumerate(individual):
            for parent_ix in parents[i]:
                if parent_ix >= 0:
                    t1 = time[i]
                    t2 = time[parent_ix]
                    if t1 >= t2:
                        raise ValueError(
                            "Ind {} has time >= than parent {}".format(
                                ind, individual[parent_ix]
                            )
                        )

    @staticmethod
    def parent_ID_to_index(individual, parent_IDs):
        n_inds, n_parents = parent_IDs.shape
        parents = np.zeros(parent_IDs.shape, dtype=int)
        ind_to_index_dict = dict(zip(individual, range(n_inds)))

        if 0 in ind_to_index_dict:
            raise ValueError(
                "Invalid ID: 0 reserved to denote individual" "not in the genealogy"
            )
        ind_to_index_dict[0] = -1

        for i in range(n_inds):
            for j in range(n_parents):
                parent_ID = parent_IDs[i, j]
                parents[i, j] = ind_to_index_dict[parent_ID]

        return parents

    @staticmethod
    def parent_index_to_ID(individual, parents):
        n_inds, n_parents = parents.shape
        parent_IDs = np.zeros(parents.shape, dtype=int)

        for i in range(n_inds):
            for j in range(n_parents):
                parent_ID = 0
                if parents[i, j] >= 0:
                    parent_ID = individual[parents[i, j]]
                    parent_IDs[i, j] = parent_ID

        return parent_IDs

    @staticmethod
    def default_format():
        cols = {
            "individual": 0,
            "parents": [1, 2],
            "time": 3,
            "is_sample": None,
            "sexes": None,
        }

        return cols

    @staticmethod
    def read_txt(pedfile, time_col=None, sex_col=None, **kwargs):
        """
        Creates a Pedigree instance from a text file.
        """
        cols = Pedigree.default_format()
        cols["time"] = time_col
        cols["sexes"] = sex_col

        if sex_col:
            raise NotImplementedError("Specifying sex of individuals not yet supported")

        usecols = []
        for c in cols.values():
            if isinstance(c, collections.Iterable):
                usecols.extend(c)
            elif c is not None:
                usecols.append(c)
        usecols = sorted(usecols)

        data = np.genfromtxt(pedfile, skip_header=1, usecols=usecols, dtype=float)

        individual = data[:, cols["individual"]].astype(int)
        parent_IDs = data[:, cols["parents"]].astype(int)
        parents = Pedigree.parent_ID_to_index(individual, parent_IDs)

        if cols["time"] is not None:
            time = data[:, cols["time"]]
        else:
            time = Pedigree.get_times(individual, parents=parents)

        return Pedigree(individual, parents, time, **kwargs)

    @staticmethod
    def read_npy(pedarray_file, **kwargs):
        """
        Reads pedigree from numpy .npy file with columns:

            ind ID, father array index, mother array index, time

        where time is given in generations.
        """
        basename, ext = os.path.split(pedarray_file)
        pedarray = np.load(pedarray_file)

        cols = Pedigree.default_format()
        if "cols" in kwargs:
            cols = kwargs["cols"]

        individual = pedarray[:, cols["individual"]]
        parents = np.stack([pedarray[:, i] for i in cols["parents"]], axis=1)
        parents = parents.astype(int)
        time = pedarray[:, cols["time"]]

        P = Pedigree(individual, parents, time, **kwargs)

        return P

    def build_array(self):
        cols = Pedigree.default_format()

        col_nums = []
        for v in cols.values():
            if isinstance(v, collections.Iterable):
                col_nums.extend(v)
            elif v is not None:
                col_nums.append(v)

        n_cols = max(col_nums) + 1

        if max(np.diff(col_nums)) > 1:
            raise ValueError(f"Non-sequential columns in pedigree format: {col_nums}")

        pedarray = np.zeros((self.num_individuals, n_cols))
        pedarray[:, cols["individual"]] = self.individual
        pedarray[:, cols["parents"]] = self.parents
        pedarray[:, cols["time"]] = self.time

        return pedarray

    def save_txt(self, fname):
        """
        Saves pedigree in text format with columns:

            ind ID, father array index, mother array index, time

        where time is given in generations.
        """
        pedarray = self.build_array()
        cols = self.default_format()
        cols_to_save = [
            cols["individual"],
            cols["parents"][0],
            cols["parents"][1],
            cols["time"],
        ]
        pedarray = pedarray[cols_to_save]
        parent_IDs = Pedigree.parent_index_to_ID(self.individual, self.parents)
        pedarray[:, cols["parents"]] = parent_IDs

        with open(fname, "w") as f:
            header = "ind\tfather\tmother\ttime\n"
            f.write(header)
            for row in pedarray:
                f.write("\t".join([str(x) for x in row]) + "\n")

    def save_npy(self, fname):
        """
        Saves pedigree in numpy .npy format with columns:

            ind ID, father array index, mother array index, time

        where time is given in generations.
        """
        pedarray = self.build_array()
        np.save(os.path.expanduser(fname), pedarray)

    def asdict(self):
        """
        Returns a dict of arguments to recreate this pedigree
        """
        return {
            key: getattr(self, key)
            for key in inspect.signature(self.__init__).parameters.keys()
            if hasattr(self, key)
        }


class DemographicEvent:
    """
    Superclass of demographic events that occur during simulations.
    """

    def __init__(self, type_, time):
        self.type = type_
        self.time = time

    def __repr__(self):
        return repr(self.__dict__)

    def asdict(self):
        return {
            key: getattr(self, key)
            for key in inspect.signature(self.__init__).parameters.keys()
            if hasattr(self, key)
        }


class PopulationParametersChange(DemographicEvent):
    """
    Changes the demographic parameters of a population at a given time.

    This event generalises the ``-eg``, ``-eG``, ``-en`` and ``-eN``
    options from ``ms``. Note that unlike ``ms`` we do not automatically
    set growth rates to zero when the population size is changed.

    :param float time: The length of time ago at which this event
        occurred.
    :param float initial_size: The absolute diploid size of the population
        at the beginning of the time slice starting at ``time``. If None,
        this is calculated according to the initial population size and
        growth rate over the preceding time slice.
    :param float growth_rate: The new per-generation growth rate. If None,
        the growth rate is not changed. Defaults to None.
    :param int population: The ID of the population affected. If
        ``population`` is None, the changes affect all populations
        simultaneously.
    """

    def __init__(
        self,
        time,
        initial_size=None,
        growth_rate=None,
        population=None,
        population_id=None,
    ):
        super().__init__("population_parameters_change", time)
        if population_id is not None and population is not None:
            raise ValueError(
                "population_id and population are aliases; cannot supply both."
            )
        if population_id is not None:
            population = population_id
        if growth_rate is None and initial_size is None:
            raise ValueError("Must specify one or more of growth_rate and initial_size")
        if initial_size is not None and initial_size <= 0:
            raise ValueError("Cannot have a population size <= 0")
        self.time = time
        self.growth_rate = growth_rate
        self.initial_size = initial_size
        self.population = -1 if population is None else population

    def get_ll_representation(self, num_populations):
        ret = {"type": self.type, "time": self.time, "population": self.population}
        if self.growth_rate is not None:
            ret["growth_rate"] = self.growth_rate
        if self.initial_size is not None:
            ret["initial_size"] = self.initial_size
        return ret

    def __str__(self):
        s = f"Population parameter change for {self.population}: "
        if self.initial_size is not None:
            s += f"initial_size -> {self.initial_size} "
        if self.growth_rate is not None:
            s += f"growth_rate -> {self.growth_rate} "
        return s


class MigrationRateChange(DemographicEvent):
    """
    Changes the rate of migration to a new value at a specific time.

    :param float time: The time at which this event occurs in generations.
    :param float rate: The new per-generation migration rate.
    :param tuple matrix_index: A tuple of two population IDs descibing
        the matrix index of interest. If ``matrix_index`` is None, all
        non-diagonal entries of the migration matrix are changed
        simultaneously.
    """

    def __init__(self, time, rate, matrix_index=None):
        super().__init__("migration_rate_change", time)
        self.rate = rate
        self.matrix_index = matrix_index

    def get_ll_representation(self, num_populations):
        matrix_index = -1
        if self.matrix_index is not None:
            matrix_index = self.matrix_index[0] * num_populations + self.matrix_index[1]
        return {
            "type": self.type,
            "time": self.time,
            "migration_rate": self.rate,
            "matrix_index": matrix_index,
        }

    def __str__(self):
        if self.matrix_index is None:
            ret = f"Migration rate change to {self.rate} everywhere"
        else:
            ret = "Migration rate change for {} to {}".format(
                self.matrix_index, self.rate
            )
        return ret


class MassMigration(DemographicEvent):
    """
    A mass migration event in which some fraction of the population in
    one deme simultaneously move to another deme, viewed backwards in
    time. Each lineage currently present in the source population
    moves to the destination population (backwards in time) with
    probability equal to ``proportion``.

    This event class generalises the population split (``-ej``) and
    admixture (``-es``) events from ``ms``. Note that MassMigrations
    do *not* have any side effects on the migration matrix.

    :param float time: The time at which this event occurs in generations.
    :param int source: The ID of the source population.
    :param int dest: The ID of the destination population.
    :param float proportion: The probability that any given lineage within
        the source population migrates to the destination population.
    """

    def __init__(self, time, source, dest=None, proportion=1.0, destination=None):
        super().__init__("mass_migration", time)
        if dest is not None and destination is not None:
            raise ValueError("dest and destination are aliases; cannot supply both")
        if destination is not None:
            dest = destination
        self.source = source
        self.dest = dest
        self.proportion = proportion

    def get_ll_representation(self, num_populations):
        return {
            "type": self.type,
            "time": self.time,
            "source": self.source,
            "dest": self.dest,
            "proportion": self.proportion,
        }

    def __str__(self):
        return (
            "Mass migration: "
            "Lineages moved with probability {} backwards in time with "
            "source {} & dest {}"
            "\n                     "
            "(equivalent to migration from {} to {} forwards in time)".format(
                self.proportion, self.source, self.dest, self.dest, self.source
            )
        )


class SimulationModelChange(DemographicEvent):
    """
    An event representing a change of underlying :ref:`simulation model
    <sec_api_simulation_models>`.

    :param float time: The time at which the simulation model changes
        to the new model, in generations. After this time, all internal
        tree nodes, edges and migrations are the result of the new model.
        If time is set to None (the default), the model change will occur
        immediately after the previous model has completed. If time is a
        callable, the time at which the simulation model changes is the result
        of calling this function with the time that the previous model
        started with as a parameter.
    :param model: The new simulation model to use.
        This can either be a string (e.g., ``"smc_prime"``) or an instance of
        a simulation model class (e.g, ``msprime.DiscreteTimeWrightFisher(100)``.
        Please see the :ref:`sec_api_simulation_models` section for more details
        on specifying simulations models. If the argument is a string, the
        reference population size is set from the top level ``Ne`` parameter
        to :func:`.simulate`. If this is None (the default) the model is
        changed to the standard coalescent with a reference_size of
        Ne (if model was not specified).
    :type model: str or simulation model instance
    """

    # Implementation note: these are treated as demographic events for the
    # sake of the high-level interface, but are treated differently at run
    # time. There is no corresponding demographic event in the C layer, as
    # this would add too much complexity to the main loops. Instead, we
    # detect these events at the high level, and insert calls to set_model
    # as appropriate.
    def __init__(self, time=None, model=None):
        super().__init__("simulation_model_change", time)
        self.model = model

    def get_ll_representation(self, num_populations):
        return {
            "type": self.type,
            "time": self.time,
            "model": self.model.get_ll_representation(),
        }

    def __str__(self):
        return f"Population model changes to {self.model}"


class SimpleBottleneck(DemographicEvent):
    # This is an unsupported/undocumented demographic event.
    def __init__(self, time, population=None, proportion=1.0, population_id=None):
        super().__init__("simple_bottleneck", time)
        if population_id is not None and population is not None:
            raise ValueError(
                "population_id and population are aliases; cannot supply both."
            )
        if population_id is not None:
            population = population_id
        self.population = population
        self.proportion = proportion

    def get_ll_representation(self, num_populations):
        return {
            "type": self.type,
            "time": self.time,
            "population": self.population,
            "proportion": self.proportion,
        }

    def __str__(self):
        return (
            "Simple bottleneck: lineages in population {} coalesce "
            "probability {}".format(self.population, self.proportion)
        )


class InstantaneousBottleneck(DemographicEvent):
    # TODO document

    def __init__(self, time, population=None, strength=1.0, population_id=None):
        super().__init__("instantaneous_bottleneck", time)
        if population_id is not None and population is not None:
            raise ValueError(
                "population_id and population are aliases; cannot supply both."
            )
        if population_id is not None:
            population = population_id
        self.population = population
        self.strength = strength

    def get_ll_representation(self, num_populations):
        return {
            "type": self.type,
            "time": self.time,
            "population": self.population,
            "strength": self.strength,
        }

    def __str__(self):
        return (
            "Instantaneous bottleneck in population {}: equivalent to {} "
            "generations of the coalescent".format(self.population, self.strength)
        )


class CensusEvent(DemographicEvent):
    """
    An event that adds a node to each branch of every tree at a given time
    during the simulation. This may be used to record all ancestral haplotypes
    present at that time, and to extract other information related to these
    haplotypes: for instance to trace the local ancestry of a sample back to a
    set of contemporaneous ancestors, or to assess whether a subset of samples
    has coalesced more recently than the census time.
    See the :ref:`tutorial<sec_tutorial_demography_census>` for an example.

    :param float time: The time at which this event occurs in generations.
    """

    def __init__(self, time):
        super().__init__("census_event", time)

    def get_ll_representation(self, num_populations):
        return {
            "type": self.type,
            "time": self.time,
        }

    def __str__(self):
        return "Census event"


class SimulationModel:
    """
    Abstract superclass of all simulation models.
    """

    name = None

    def __init__(self, reference_size=None):
        self.reference_size = reference_size

    def get_ll_representation(self):
        return {"name": self.name, "reference_size": self.reference_size}

    def __str__(self):
        return f"{self.name}(reference_size={self.reference_size})"

    def asdict(self):
        return {
            key: getattr(self, key)
            for key in inspect.signature(self.__init__).parameters.keys()
            if hasattr(self, key)
        }


class StandardCoalescent(SimulationModel):
    """
    The classical coalescent with recombination model (i.e., Hudson's algorithm).
    The string ``"hudson"`` can be used to refer to this model.

    This is the default simulation model.
    """

    name = "hudson"


class SmcApproxCoalescent(SimulationModel):
    """
    The original SMC model defined by McVean and Cardin. This
    model is implemented using a naive rejection sampling approach
    and so it may not be any more efficient to simulate than the
    standard Hudson model.

    The string ``"smc"`` can be used to refer to this model.
    """

    name = "smc"


class SmcPrimeApproxCoalescent(SimulationModel):
    """
    The SMC' model defined by Marjoram and Wall as an improvement on the
    original SMC. model is implemented using a naive rejection sampling
    approach and so it may not be any more efficient to simulate than the
    standard Hudson model.

    The string ``"smc_prime"`` can be used to refer to this model.
    """

    name = "smc_prime"


class DiscreteTimeWrightFisher(SimulationModel):
    """
    A discrete backwards-time Wright-Fisher model, with diploid back-and-forth
    recombination. The string ``"dtwf"`` can be used to refer to this model.

    Wright-Fisher simulations are performed very similarly to coalescent
    simulations, with all parameters denoting the same quantities in both
    models. Because events occur at discrete times however, the order in which
    they occur matters. Each generation consists of the following ordered
    events:

    - Migration events. As in the Hudson coalescent, these move single extant
      lineages between populations. Because migration events occur before
      lineages choose parents, migrant lineages choose parents from their new
      population in the same generation.
    - Demographic events. All events with `previous_generation < event_time <=
      current_generation` are carried out here.
    - Lineages draw parents. Each (monoploid) extant lineage draws a parent
      from their current population.
    - Diploid recombination. Each parent is diploid, so all child lineages
      recombine back-and-forth into the same two parental genome copies. These
      become two independent lineages in the next generation.
    - Historical sampling events. All historical samples with
      `previous_generation < sample_time <= current_generation` are inserted.

    """

    name = "dtwf"


class WrightFisherPedigree(SimulationModel):
    # TODO Complete documentation.
    """
    Backwards-time simulations through a pre-specified pedigree, with diploid
    individuals and back-and-forth recombination. The string ``"wf_ped"`` can
    be used to refer to this model.
    """
    name = "wf_ped"


class ParametricSimulationModel(SimulationModel):
    """
    The superclass of simulation models that require extra parameters.
    """

    def get_ll_representation(self):
        d = super().get_ll_representation()
        d.update(self.__dict__)
        return d


class BetaCoalescent(ParametricSimulationModel):
    """
    A diploid Xi-coalescent with up to four simultaneous multiple mergers and
    crossover recombination.

    There are two main differences between the Beta-Xi-coalescent and the
    standard coalescent. Firstly, the number of lineages that take part in each
    common ancestor event is random, with distribution determined by moments of
    the :math:`Beta(2 - \\alpha, \\alpha)`-distribution. In particular, when there
    are :math:`n` lineages, each set of :math:`k \\leq n` of them participates in a
    common ancestor event at rate

    .. math::
        \\frac{8}{B(2 - \\alpha, \\alpha)}
        \\int_0^1 x^{k - \\alpha - 1} (1 - x)^{n - k + \\alpha - 1} dx,

    where :math:`B(2 - \\alpha, \\alpha)` is the Beta-function.

    In a common ancestor event, all participating lineages are randomly split
    into four groups, corresponding to the four parental chromosomes in a diploid,
    bi-parental reproduction event. All lineages within each group merge simultaneously.

    .. warning::
        The prefactor of 8 in the common ancestor event rate arises as the product
        of two terms. A factor of 4 compensates for the fact that only one quarter
        of binary common ancestor events result in a merger due to diploidy.
        A further factor of 2 is included for consistency with the implementation
        of the Hudson model, in which :math:`n` lineages undergo binary mergers at
        rate :math:`n (n - 1)`.

    Secondly, the time scale predicted by the Beta-Xi-coalescent is proportional
    to :math:`N_e^{\\alpha - 1}` generations, where :math:`N_e` is the effective
    population size. Specifically, one unit of coalescent time corresponds to
    a number of generations given by

    .. math::
        \\frac{m^{\\alpha} N_e^{\\alpha - 1}}{\\alpha B(2 - \\alpha, \\alpha)},

    where

    .. math::
        m = 2 + \\frac{2^{\\alpha}}{3^{\\alpha - 1} (\\alpha - 1)}.

    Note that the time scale depends both on the effective population size :math:`N_e`
    and :math:`\\alpha`, and can be dramatically shorter than the timescale of the
    standard coalescent. Thus, effective population sizes must often be many orders
    of magnitude larger than census population sizes. The per-generation recombination
    rate is rescaled similarly to obtain the population-rescaled recombination rate.

    See `Schweinsberg (2003)
    <https://www.sciencedirect.com/science/article/pii/S0304414903000280>`_
    for the derivation of the common ancestor event rate, as well as the time scaling.
    Note however that the model of Schweinsberg (2003) is haploid, so that
    all participating lineages merge in a common ancestor event without
    splitting into four groups.

    :param float alpha: Determines the degree of skewness in the family size
        distribution, and must satisfy :math:`1 < \\alpha < 2`. Smaller values of
        :math:`\\alpha` correspond to greater skewness, and :math:`\\alpha = 2`
        would coincide with the standard coalescent.
    :param float truncation_point: Determines the maximum fraction of the
        population replaced by offspring in one reproduction event, and must
        satisfy :math:`0 < \\tau \\leq 1`, where :math:`\\tau` is the truncation point.
        The default is :math:`\\tau = 1`, which corresponds to the standard
        Beta-Xi-coalescent. When :math:`\\tau < 1`, the number of lineages
        participating in a common ancestor event is determined by moments
        of the :math:`Beta(2 - \\alpha, \\alpha)` distribution conditioned on not
        exceeding :math:`\\tau`, and the Beta-function in the expression
        for the time scale is also replaced by the incomplete Beta function
        :math:`Beta(\\tau; 2 - \\alpha, \\alpha)`.
    """

    name = "beta"

    def __init__(self, reference_size=None, alpha=None, truncation_point=1):
        super().__init__(reference_size=reference_size)
        self.alpha = alpha
        self.truncation_point = truncation_point


class DiracCoalescent(ParametricSimulationModel):
    """
    A diploid Xi-coalescent with up to four simultaneous multiple mergers and
    crossover recombination.

    The Dirac-Xi-coalescent is an implementation of the model of
    `Blath et al. (2013) <https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3527250/>`_
    The simulation proceeds similarly to the standard coalescent.
    In addition to binary common ancestor events at rate :math:`n (n - 1)` when
    there are :math:`n` lineages, potential multiple merger events take place
    at rate :math:`2 c > 0`. Each lineage participates in each multiple merger
    event independently with probability :math:`0 < \\psi \\leq 1`. All participating
    lineages are randomly split into four groups, corresponding to the four
    parental chromosomes present in a diploid, bi-parental reproduction event,
    and the lineages within each group merge simultaneously.

    .. warning::
        The Dirac-Xi-coalescent is obtained as a scaling limit of Moran models,
        rather than Wright-Fisher models. As a consequence, one unit of coalescent
        time is proportional to :math:`N_e^2` generations,
        rather than :math:`N_e` generations as in the standard coalescent.
        However, the coalescent recombination rate is obtained from the
        per-generation recombination probability by rescaling with
        :math:`N_e`. See :ref:`sec_tutorial_multiple_mergers`
        for an illustration of how this affects simulation output in practice.

    :param float c: Determines the rate of potential multiple merger events.
        We require :math:`c > 0`.
    :param float psi: Determines the fraction of the population replaced by
        offspring in one large reproduction event, i.e. one reproduction event
        giving rise to potential multiple mergers when viewed backwards in time.
        We require :math:`0 < \\psi \\leq 1`.
    """

    name = "dirac"

    def __init__(self, reference_size=None, psi=None, c=None):
        super().__init__(reference_size=reference_size)
        self.psi = psi
        self.c = c


class SweepGenicSelection(ParametricSimulationModel):
    # TODO document
    name = "sweep_genic_selection"

    # TODO Probably want to rethink the parameters here. Probably these should
    # be kw-only?
    def __init__(
        self,
        position,
        start_frequency,
        end_frequency,
        alpha,
        dt=None,
        reference_size=None,
    ):
        super().__init__(reference_size=reference_size)
        # We might want to have a default dt value that depends on the other
        # parameters.
        if dt is None:
            dt = 0.01
        self.position = position
        self.start_frequency = start_frequency
        self.end_frequency = end_frequency
        self.alpha = alpha
        self.dt = dt


class PopulationParameters:
    """
    Simple class to represent the state of a population in terms of its
    demographic parameters.
    """

    def __init__(self, start_size, end_size, growth_rate):
        self.start_size = start_size
        self.end_size = end_size
        self.growth_rate = growth_rate

    def __repr__(self):
        return repr(self.__dict__)


class Epoch:
    """
    Represents a single epoch in the simulation within which the state
    of the demographic parameters are constant.
    """

    def __init__(
        self,
        start_time=None,
        end_time=None,
        populations=None,
        migration_matrix=None,
        demographic_events=None,
    ):
        self.start_time = start_time
        self.end_time = end_time
        self.populations = populations
        self.migration_matrix = migration_matrix
        self.demographic_events = demographic_events

    def __repr__(self):
        return repr(self.__dict__)


def _matrix_exponential(A):
    """
    Returns the matrix exponential of A.
    https://en.wikipedia.org/wiki/Matrix_exponential
    Note: this is not a general purpose method and is only intended for use within
    msprime.
    """
    d, Y = np.linalg.eig(A)
    Yinv = np.linalg.pinv(Y)
    D = np.diag(np.exp(d))
    B = np.matmul(Y, np.matmul(D, Yinv))
    return np.real_if_close(B, tol=1000)


class DemographyDebugger:
    """
    A class to facilitate debugging of population parameters and migration
    rates in the past.
    """

    def __init__(
        self,
        Ne=1,
        population_configurations=None,
        migration_matrix=None,
        demographic_events=None,
        model="hudson",
    ):
        if demographic_events is None:
            demographic_events = []
        self.demographic_events = demographic_events
        self._precision = 3
        # Make sure that we have a sample size of at least 2 so that we can
        # initialise the simulator.
        sample_size = None
        if population_configurations is None:
            sample_size = 2
        else:
            saved_sample_sizes = [
                pop_config.sample_size for pop_config in population_configurations
            ]
            for pop_config in population_configurations:
                pop_config.sample_size = 2
        simulator = simulator_factory(
            sample_size=sample_size,
            model=model,
            Ne=Ne,
            population_configurations=population_configurations,
            migration_matrix=migration_matrix,
            demographic_events=demographic_events,
        )
        if len(simulator.model_change_events) > 0:
            raise ValueError(
                "Model changes not currently supported by the DemographyDebugger. "
                "Please open an issue on GitHub if this feature would be useful to you"
            )
        assert len(simulator.model_change_events) == 0
        self._make_epochs(simulator, sorted(demographic_events, key=lambda e: e.time))
        self.simulation_model = simulator.model

        if population_configurations is not None:
            # Restore the saved sample sizes.
            for pop_config, sample_size in zip(
                population_configurations, saved_sample_sizes
            ):
                pop_config.sample_size = sample_size
        self.migration_matrix = migration_matrix
        self.population_configurations = population_configurations

    def _make_epochs(self, simulator, demographic_events):
        self.epochs = []
        self.num_populations = simulator.num_populations
        ll_sim = simulator.create_ll_instance()
        N = simulator.num_populations
        start_time = 0
        end_time = 0
        abs_tol = 1e-9
        event_index = 0
        while not math.isinf(end_time):
            events = []
            while event_index < len(demographic_events) and utils.almost_equal(
                demographic_events[event_index].time, start_time, abs_tol=abs_tol
            ):
                events.append(demographic_events[event_index])
                event_index += 1
            end_time = ll_sim.debug_demography()
            m = ll_sim.get_migration_matrix()
            migration_matrix = [[m[j * N + k] for k in range(N)] for j in range(N)]
            growth_rates = [
                conf["growth_rate"] for conf in ll_sim.get_population_configuration()
            ]
            populations = [
                PopulationParameters(
                    start_size=ll_sim.compute_population_size(j, start_time),
                    end_size=ll_sim.compute_population_size(j, end_time),
                    growth_rate=growth_rates[j],
                )
                for j in range(N)
            ]
            self.epochs.append(
                Epoch(start_time, end_time, populations, migration_matrix, events)
            )
            start_time = end_time

    def _print_populations(self, epoch, output):
        field_width = self._precision + 6
        growth_rate_field_width = 14
        sep_str = " | "
        N = len(epoch.migration_matrix)
        fmt = (
            "{id:<2} "
            "{start_size:^{field_width}}"
            "{end_size:^{field_width}}"
            "{growth_rate:>{growth_rate_field_width}}"
        )
        print(
            fmt.format(
                id="",
                start_size="start",
                end_size="end",
                growth_rate="growth_rate",
                field_width=field_width,
                growth_rate_field_width=growth_rate_field_width,
            ),
            end=sep_str,
            file=output,
        )
        for k in range(N):
            print("{0:^{1}}".format(k, field_width), end="", file=output)
        print(file=output)
        h = "-" * (field_width - 1)
        print(
            fmt.format(
                id="",
                start_size=h,
                end_size=h,
                growth_rate=h,
                field_width=field_width,
                growth_rate_field_width=growth_rate_field_width,
            ),
            end=sep_str,
            file=output,
        )
        for k in range(N):
            s = "-" * (field_width - 1)
            print("{0:<{1}}".format(s, field_width), end="", file=output)
        print(file=output)
        for j, pop in enumerate(epoch.populations):
            s = (
                "{id:<2}|"
                "{start_size:^{field_width}.{precision}g}"
                "{end_size:^{field_width}.{precision}g}"
                "{growth_rate:>{growth_rate_field_width}.{precision}g}"
            ).format(
                id=j,
                start_size=pop.start_size,
                end_size=pop.end_size,
                growth_rate=pop.growth_rate,
                precision=self._precision,
                field_width=field_width,
                growth_rate_field_width=growth_rate_field_width,
            )
            print(s, end=sep_str, file=output)
            for k in range(N):
                x = epoch.migration_matrix[j][k]
                print(
                    "{0:^{1}.{2}g}".format(x, field_width, self._precision),
                    end="",
                    file=output,
                )
            print(file=output)

    def print_history(self, output=sys.stdout):
        """
        Prints a summary of the history of the populations.
        """
        print("Model = ", self.simulation_model, file=output)
        for epoch in self.epochs:
            if len(epoch.demographic_events) > 0:
                print(f"Events @ generation {epoch.start_time}", file=output)
            for event in epoch.demographic_events:
                print("   -", event, file=output)
            s = f"Epoch: {epoch.start_time} -- {epoch.end_time} generations"
            print("=" * len(s), file=output)
            print(s, file=output)
            print("=" * len(s), file=output)
            self._print_populations(epoch, output)
            print(file=output)

    def population_size_trajectory(self, steps):
        """
        This function returns an array of per-population effective population sizes,
        as defined by the demographic model. These are the `initial_size`
        parameters of the model, modified by any population growth rates.
        The sizes are computed at the time points given by `steps`.

        :param list steps: List of times ago at which the population
            size will be computed.
        :return: Returns a numpy array of population sizes, with one column per
            population, whose [i,j]th entry is the size of population
            j at time steps[i] ago.
        """
        num_pops = self.num_populations
        N_t = np.zeros([len(steps), num_pops])
        for j, t in enumerate(steps):
            N, _ = self._pop_size_and_migration_at_t(t)
            N_t[j] = N
        return N_t

    def possible_lineages(self, samples=None):
        """
        Given the sampling configuration, this function determines when lineages are
        possibly found within each population over epochs defined by demographic events
        and sampling times. If no sampling configuration is given, we assume we sample
        lineages from each population at time zero. The samples are specified by a list
        of msprime Sample objects, so that possible ancient samples may be accounted for.

        :param list samples: A list of msprime Sample objects, which specify their
            populations and times.
        :return: Returns a dictionary of times defining the epoch intervals with arrays
            of length equal to the number of populations and containing 1s or 0s for
            whether lineages could be found in that population.
        """

        def reachable_through_migration(mig_mat, epoch_lineages):
            # get connected populations via migration
            reachable = (
                (
                    np.linalg.matrix_power(
                        np.eye(self.num_populations) + mig_mat, self.num_populations
                    )
                    > 0
                )
                * 1
            ).T  # should this be transposed? check migration matrix direction
            return (reachable.dot(epoch_lineages) > 0) * 1

        # get configuration of sampling times from samples ({time:[pops_sampled_from]})
        if samples is None:
            sampling_times = {0: [i for i in range(self.num_populations)]}
        else:
            sampling_times = collections.defaultdict(list)
            for sample in samples:
                sampling_times[sample.time].append(sample.population)
            for t in sampling_times.keys():
                sampling_times[t] = list(set(sampling_times[t]))

        # initial epoch and lineage placements
        lineages = {
            0: np.array(
                [
                    1 if i in sampling_times[0] else 0
                    for i in range(self.num_populations)
                ]
            )
        }

        # Iterate through demographic events: mass migration with frac=1 turns off
        # lineages in the source deme and turns on lineages in the dest deme, continuous
        # migration turns on lineages in demes that are reachable, and changes in
        # population sizes don't do anything (ignored)
        # If we've jumped to the next epoch, we have to first check if there
        # have been ancient samples in the previous epoch.
        current_time = 0
        current_epoch_num = 0
        mig_mat = self.epochs[0].migration_matrix
        for de in self.demographic_events:
            if de.time > current_time:
                # jump to next interval, but we first need to turn on lineages through
                # migration, and then check if there have been ancient sampling events
                # in the last epoch
                lineages[current_time] = reachable_through_migration(
                    mig_mat, lineages[current_time]
                )
                for t in sorted(sampling_times):
                    if current_time < t <= de.time:
                        lineages[t] = copy.copy(lineages[current_time])
                        for deme in sampling_times[t]:
                            lineages[t][deme] = 1
                        current_time = t
                        # ancient sampling could result in additional reachable demes
                        lineages[current_time] = reachable_through_migration(
                            mig_mat, lineages[current_time]
                        )
                # update migration matrix
                current_epoch_num += 1
                mig_mat = self.epochs[current_epoch_num].migration_matrix
                lineages[de.time] = copy.copy(lineages[current_time])
                current_time = de.time
            if de.type == "mass_migration":
                # turn on and off lineages due to mass migration events
                # only turn on if source deme has lineages possible
                last = max(lineages.keys())
                if lineages[last][de.source] == 1:
                    lineages[current_time][de.dest] = 1
                    if de.proportion == 1:
                        lineages[current_time][de.source] = 0

        # finally, ensure that the final epoch accounts for migration
        lineages[current_time] = reachable_through_migration(
            mig_mat, lineages[current_time]
        )

        # combine epochs if there have not been changes in state
        combined_lineages = {}
        combined_lineages[0] = lineages[0]
        for epoch_time in sorted(lineages.keys())[1:]:
            last = max(combined_lineages.keys())
            if np.any(lineages[epoch_time] != combined_lineages[last]):
                combined_lineages[epoch_time] = lineages[epoch_time]

        return combined_lineages

    def calculate_lineage_probabilities(self, steps, sample_time=0):
        """
        Returns an array such that P[j, a, b] is the probability that a lineage that
        started in population a at time sample_time is in population b at time steps[j]
        ago.

        This function reports sampling probabilities _before_ mass migration events
        at a step time, if a mass migration event occurs at one of those times.
        Migrations will then effect then next time step

        :param list steps: A list of times to compute probabilities of lineages.
        :param sample_time: The time of sampling of the lineage. For any times in steps
            that are more recent than sample_time, the probability of finding the
            lineage in any population is zero.
        :return: An array of dimension len(steps) by num pops by num_pops.
        """
        num_pops = self.num_populations
        # P[i, j] will be the probability that a lineage that started in i is now in j
        P = np.zeros([num_pops, num_pops])

        for ii in range(num_pops):
            P[ii, ii] = 1

        # epochs are defined by mass migration events or changes to population sizes
        # or migration rates, so we add the epoch interval times to the steps that we
        # need to account for
        epoch_breaks = [t for t in self.epoch_times if t not in steps]
        all_steps = np.concatenate([steps, epoch_breaks])

        # add sample time if not in epoch_breaks
        sampling = []
        if sample_time not in all_steps:
            sampling.append(sample_time)
        all_steps = np.concatenate((all_steps, sampling))

        ix = np.argsort(all_steps)
        all_steps = all_steps[ix]
        # keep track of the steps to report in P_out
        keep_steps = np.concatenate(
            [
                np.repeat(True, len(steps)),
                np.repeat(False, len(epoch_breaks)),
                np.repeat(False, len(sampling)),
            ]
        )[ix]

        assert len(np.unique(all_steps)) == len(all_steps)
        assert np.all(steps == all_steps[keep_steps])
        P_out = np.zeros((len(all_steps), num_pops, num_pops))

        first_step = 0
        while all_steps[first_step] < sample_time:
            first_step += 1

        P_out[first_step] = P

        # get ordered mass migration events
        mass_migration_objects = []
        mass_migration_times = []
        for demo in self.demographic_events:
            if demo.type == "mass_migration":
                mass_migration_objects.append(demo)
                mass_migration_times.append(demo.time)

        for jj in range(first_step, len(all_steps) - 1):
            t_j = all_steps[jj]

            # apply any mass migration events to P
            # so if we sample at this time, we do no account for the instantaneous
            # mass migration events that occur at the same time. that will show up
            # at the next step
            for mass_mig_t, mass_mig_e in zip(
                mass_migration_times, mass_migration_objects
            ):
                if mass_mig_t == t_j:
                    S = np.eye(num_pops, num_pops)
                    S[mass_mig_e.source, mass_mig_e.dest] = mass_mig_e.proportion
                    S[mass_mig_e.source, mass_mig_e.source] = 1 - mass_mig_e.proportion
                    P = np.matmul(P, S)

            # get continuous migration matrix over next interval
            _, M = self._pop_size_and_migration_at_t(t_j)
            dt = all_steps[jj + 1] - all_steps[jj]
            dM = np.diag([sum(s) for s in M])
            # advance to next interval time (dt) taking into account continuous mig
            P = P.dot(_matrix_exponential(dt * (M - dM)))
            P_out[jj + 1] = P

        return P_out[keep_steps]

    def indicators_from_probabilities(self, samples=None):
        """
        Given the sampling configuration, this function determines when lineages are
        possibly found within each population over epochs defined by demographic events
        and sampling times. If no sampling configuration is given, we assume we sample
        lineages from each population at time zero. The samples are specified by a list
        of msprime Sample objects, so that possible ancient samples may be accounted for.

        Samples: A list of msprime Sample objects, which specify their
            populations and times.
        Returns a dictionary with epochs as keys, array of zeros and ones for possible
            presence of lineages.
        """
        # get configuration of sampling times from samples ({time:[pops_sampled_from]})
        if samples is None:
            sampling_times = {0: [i for i in range(self.num_populations)]}
        else:
            sampling_times = collections.defaultdict(list)
            for sample in samples:
                sampling_times[sample.time].append(sample.population)
            for t in sampling_times.keys():
                sampling_times[t] = list(set(sampling_times[t]))

        all_steps = sorted(
            list(set([e.start_time for e in self.epochs] + list(sampling_times.keys())))
        )

        epochs = [(x, y) for x, y in zip(all_steps[:-1], all_steps[1:])]
        epochs.append((all_steps[-1], np.inf))

        # need to go a bit beyond last step and into the final epoch that extends to inf
        all_steps.append(all_steps[-1] + 1)

        indicators = {e: np.zeros(self.num_populations).astype(int) for e in epochs}
        for sample_time, demes in sampling_times.items():
            P_out = self.calculate_lineage_probabilities(
                all_steps, sample_time=sample_time
            )
            for epoch, P in zip(epochs, P_out[1:]):
                if epoch[1] <= sample_time:
                    # samples shouldn't affect the epoch previous to the sampling time
                    continue
                for deme in demes:
                    indicators[epoch][P[deme] > 0] = 1

        # join epochs if adjacent epochs have same set of possible live populations
        combined_indicators = {}
        skip = 0
        for ii, (epoch, inds) in enumerate(indicators.items()):
            if skip > 0:
                skip -= 1
                continue
            this_epoch = epoch
            while ii + skip + 1 < len(epochs) and np.all(
                indicators[epochs[ii + 1 + skip]] == inds
            ):
                this_epoch = (this_epoch[0], epochs[ii + 1 + skip][1])
                skip += 1
            combined_indicators[this_epoch] = inds

        return combined_indicators

    def mean_coalescence_time(
        self, num_samples, min_pop_size=1, steps=None, rtol=0.005, max_iter=12
    ):
        """
        Compute the mean time until coalescence between lineages of two samples drawn
        from the sample configuration specified in `num_samples`. This is done using
        :meth:`coalescence_rate_trajectory
        <.DemographyDebugger.coalescence_rate_trajectory>`
        to compute the probability that the lineages have not yet coalesced by time `t`,
        and using these to approximate :math:`E[T] = \\int_t^\\infty P(T > t) dt`,
        where :math:`T` is the coalescence time. See
        :meth:`coalescence_rate_trajectory
        <.DemographyDebugger.coalescence_rate_trajectory>`
        for more details.

        To compute this, an adequate time discretization must be arrived at
        by iteratively extending or refining the current discretization.
        Debugging information about numerical convergence of this procedure is
        logged using the Python :mod:`logging` infrastructure. To make it appear, using
        the :mod:`daiquiri` module, do for instance::

            import daiquiri

            daiquiri.setup(level="DEBUG")
            debugger.mean_coalescence_time([2])

        will print this debugging information to stderr. Briefly, this outputs
        iteration number, mean coalescence time, maximum difference in probabilty
        of not having coalesced yet, difference to last coalescence time,
        probability of not having coalesced by the final time point, and
        whether the last iteration was an extension or refinement.

        :param list num_samples: A list of the same length as the number
            of populations, so that `num_samples[j]` is the number of sampled
            chromosomes in subpopulation `j`.
        :param int min_pop_size: See :meth:`coalescence_rate_trajectory
            <.DemographyDebugger.coalescence_rate_trajectory>`.
        :param list steps: The time discretization to start out with (by default,
            picks something based on epoch times).
        :param float rtol: The relative tolerance to determine mean coalescence time
            to (used to decide when to stop subdividing the steps).
        :param int max_iter: The maximum number of times to subdivide the steps.
        :return: The mean coalescence time (a number).
        :rtype: float
        """

        def mean_time(steps, P):
            # Mean is int_0^infty P(T > t) dt, which we estimate by discrete integration
            # assuming that f(t) = P(T > t) is piecewise exponential:
            # if f(u) = a exp(bu) then b = log(f(t)/f(s)) / (t-s) for each s < t, so
            # \int_s^t f(u) du = (a/b) \int_s^t exp(bu) b du = (a/b)(exp(bt) - exp(bs))
            #    = (t - s) * (f(t) - f(s)) / log(f(t) / f(s))
            # unless b = 0, of course.
            assert steps[0] == 0
            dt = np.diff(steps)
            dP = np.diff(P)
            dlogP = np.diff(np.log(P))
            nz = np.logical_and(dP < 0, P[1:] * P[:-1] > 0)
            const = dP == 0
            return np.sum(dt[const] * (P[:-1])[const]) + np.sum(
                dt[nz] * dP[nz] / dlogP[nz]
            )

        if steps is None:
            last_N = max(self.population_size_history[:, self.num_epochs - 1])
            last_epoch = max(self.epoch_times)
            steps = sorted(
                list(
                    set(np.linspace(0, last_epoch + 12 * last_N, 101)).union(
                        set(self.epoch_times)
                    )
                )
            )
        p_diff = m_diff = np.inf
        last_P = np.inf
        step_type = "none"
        n = 0
        logger.debug(
            "iter    mean    P_diff    mean_diff last_P    adjust_type"
            "num_steps  last_step"
        )
        # The factors of 20 here are probably not optimal: clearly, we need to
        # compute P accurately, but there's no good reason for this stopping rule.
        # If populations have picewise constant size then we shouldn't need this:
        # setting steps equal to the epoch boundaries should suffice; while if
        # there is very fast exponential change in some epochs caution is needed.
        while n < max_iter and (
            last_P > rtol or p_diff > rtol / 20 or m_diff > rtol / 20
        ):
            last_steps = steps
            _, P1 = self.coalescence_rate_trajectory(
                steps=last_steps,
                num_samples=num_samples,
                min_pop_size=min_pop_size,
                double_step_validation=False,
            )
            m1 = mean_time(last_steps, P1)
            if last_P > rtol:
                step_type = "extend"
                steps = np.concatenate(
                    [steps, np.linspace(steps[-1], steps[-1] * 1.2, 20)[1:]]
                )
            else:
                step_type = "refine"
                inter = steps[:-1] + np.diff(steps) / 2
                steps = np.concatenate([steps, inter])
                steps.sort()
            _, P2 = self.coalescence_rate_trajectory(
                steps=steps,
                num_samples=num_samples,
                min_pop_size=min_pop_size,
                double_step_validation=False,
            )
            m2 = mean_time(steps, P2)
            keep_steps = np.in1d(steps, last_steps)
            p_diff = max(np.abs(P1 - P2[keep_steps]))
            m_diff = np.abs(m1 - m2) / m2
            last_P = P2[-1]
            n += 1
            # Use the old-style string formatting as this is the logging default
            logger.debug(
                "%d %g %g %g %g %s %d %d",
                n,
                m2,
                p_diff,
                m_diff,
                last_P,
                step_type,
                len(steps),
                max(steps),
            )

        if n == max_iter:
            raise ValueError(
                "Did not converge on an adequate discretisation: "
                "Increase max_iter or rtol. Consult the log for "
                "debugging information"
            )
        return m2

    def coalescence_rate_trajectory(
        self, steps, num_samples, min_pop_size=1, double_step_validation=True
    ):
        """
        This function will calculate the mean coalescence rates and proportions
        of uncoalesced lineages between the lineages of the sample
        configuration provided in `num_samples`, at each of the times ago
        listed by steps, in this demographic model. The coalescence rate at
        time t in the past is the average rate of coalescence of
        as-yet-uncoalesed lineages, computed as follows: let :math:`p(t)` be
        the probability that the lineages of a randomly chosen pair of samples
        has not yet coalesced by time :math:`t`, let :math:`p(z,t)` be the
        probability that the lineages of a randomly chosen pair of samples has
        not yet coalesced by time :math:`t` *and* are both in population
        :math:`z`, and let :math:`N(z,t)` be the diploid effective population
        size of population :math:`z` at time :math:`t`. Then the mean
        coalescence rate at time :math:`t` is :math:`r(t) = (\\sum_z p(z,t) /
        (2 * N(z,t)) / p(t)`.

        The computation is done by approximating population size trajectories
        with piecewise constant trajectories between each of the steps. For
        this to be accurate, the distance between the steps must be small
        enough so that (a) short epochs (e.g., bottlenecks) are not missed, and
        (b) populations do not change in size too much over that time, if they
        are growing or shrinking. This function optionally provides a simple
        check of this approximation by recomputing the coalescence rates on a
        grid of steps twice as fine and throwing a warning if the resulting
        values do not match to a relative tolerance of 0.001.

        :param list steps: The times ago at which coalescence rates will be computed.
        :param list num_samples: A list of the same length as the number
            of populations, so that `num_samples[j]` is the number of sampled
            chromosomes in subpopulation `j`.
        :param int min_pop_size: The smallest allowed population size during
            computation of coalescent rates (i.e., coalescence rates are actually
            1 / (2 * max(min_pop_size, N(z,t))). Spurious very small population sizes
            can occur in models where populations grow exponentially but are unused
            before some time in the past, and lead to floating point error.
            This should be set to a value smaller than the smallest
            desired population size in the model.
        :param bool double_step_validation: Whether to perform the check that
            step sizes are sufficiently small, as described above. This is highly
            recommended, and will take at most four times the computation.
        :return: A tuple of arrays whose jth elements, respectively, are the
            coalescence rate at the jth time point (denoted r(t[j]) above),
            and the probablility that a randomly chosen pair of lineages has
            not yet coalesced (denoted p(t[j]) above).
        :rtype: (numpy.array, numpy.array)
        """
        num_pops = self.num_populations
        if not len(num_samples) == num_pops:
            raise ValueError(
                "`num_samples` must have the same length as the number of populations"
            )
        steps = np.array(steps)
        if not np.all(np.diff(steps) > 0):
            raise ValueError("`steps` must be a sequence of increasing times.")
        if np.any(steps < 0):
            raise ValueError("`steps` must be non-negative")
        r, p_t = self._calculate_coalescence_rate_trajectory(
            steps=steps, num_samples=num_samples, min_pop_size=min_pop_size
        )
        if double_step_validation:
            inter = steps[:-1] + np.diff(steps) / 2
            double_steps = np.concatenate([steps, inter])
            double_steps.sort()
            rd, p_td = self._calculate_coalescence_rate_trajectory(
                steps=double_steps, num_samples=num_samples, min_pop_size=min_pop_size
            )
            assert np.all(steps == double_steps[::2])
            r_prediction_close = np.allclose(r, rd[::2], rtol=1e-3)
            p_prediction_close = np.allclose(p_t, p_td[::2], rtol=1e-3)
            if not (r_prediction_close and p_prediction_close):
                warnings.warn(
                    "Doubling the number of steps has resulted in different "
                    " predictions, please re-run with smaller step sizes to ensure "
                    " numerical accuracy."
                )
        return r, p_t

    def _calculate_coalescence_rate_trajectory(self, steps, num_samples, min_pop_size):
        num_pops = self.num_populations
        P = np.zeros([num_pops ** 2, num_pops ** 2])
        IA = np.array(range(num_pops ** 2)).reshape([num_pops, num_pops])
        Identity = np.eye(num_pops)
        for x in range(num_pops):
            for y in range(num_pops):
                P[IA[x, y], IA[x, y]] = num_samples[x] * (num_samples[y] - (x == y))
        P = P / np.sum(P)
        # add epoch breaks if not there already but remember which steps they are
        epoch_breaks = list(
            set([0.0] + [t for t in self.epoch_times if t not in steps])
        )
        steps_b = np.concatenate([steps, epoch_breaks])
        ix = np.argsort(steps_b)
        steps_b = steps_b[ix]
        keep_steps = np.concatenate(
            [np.repeat(True, len(steps)), np.repeat(False, len(epoch_breaks))]
        )[ix]
        assert np.all(steps == steps_b[keep_steps])
        mass_migration_objects = []
        mass_migration_times = []
        for demo in self.demographic_events:
            if type(demo) == MassMigration:
                mass_migration_objects.append(demo)
                mass_migration_times.append(demo.time)
        num_steps = len(steps_b)
        # recall that steps_b[0] = 0.0
        r = np.zeros(num_steps)
        p_t = np.zeros(num_steps)
        for j in range(num_steps - 1):
            time = steps_b[j]
            dt = steps_b[j + 1] - steps_b[j]
            N, M = self._pop_size_and_migration_at_t(time)
            C = np.zeros([num_pops ** 2, num_pops ** 2])
            for idx in range(num_pops):
                C[IA[idx, idx], IA[idx, idx]] = 1 / (2 * max(min_pop_size, N[idx]))
            dM = np.diag([sum(s) for s in M])
            if time in mass_migration_times:
                idx = mass_migration_times.index(time)
                a = mass_migration_objects[idx].source
                b = mass_migration_objects[idx].dest
                p = mass_migration_objects[idx].proportion
                S = np.eye(num_pops ** 2, num_pops ** 2)
                for x in range(num_pops):
                    if x == a:
                        S[IA[a, a], IA[a, b]] = S[IA[a, a], IA[b, a]] = p * (1 - p)
                        S[IA[a, a], IA[b, b]] = p ** 2
                        S[IA[a, a], IA[a, a]] = (1 - p) ** 2
                    else:
                        S[IA[x, a], IA[x, b]] = S[IA[a, x], IA[b, x]] = p
                        S[IA[x, a], IA[x, a]] = S[IA[a, x], IA[a, x]] = 1 - p
                P = np.matmul(P, S)
            p_t[j] = np.sum(P)
            r[j] = np.sum(np.matmul(P, C)) / np.sum(P)
            G = (np.kron(M - dM, Identity) + np.kron(Identity, M - dM)) - C
            P = np.matmul(P, _matrix_exponential(dt * G))
        p_t[num_steps - 1] = np.sum(P)
        r[num_steps - 1] = np.sum(np.matmul(P, C)) / np.sum(P)
        return r[keep_steps], p_t[keep_steps]

    def _pop_size_and_migration_at_t(self, t):
        """
        Returns a tuple (N, M) of population sizes (N) and migration rates (M) at
        time t ago.

        Note: this isn't part of the external API as it is be better to provide
        separate methods to access the population size and migration rates, and
        needing both together is specialised for internal calculations.

        :param float t: The time ago.
        :return: A tuple of arrays, of the same form as the population sizes and
            migration rate arrays of the demographic model.
        """
        j = 0
        while self.epochs[j].end_time <= t:
            j += 1
        N = self.population_size_history[:, j]
        for i, pop in enumerate(self.epochs[j].populations):
            s = t - self.epochs[j].start_time
            g = pop.growth_rate
            N[i] *= np.exp(-1 * g * s)
        return N, self.epochs[j].migration_matrix

    @property
    def population_size_history(self):
        """
        Returns a (num_pops, num_epochs) numpy array giving the starting population size
        for each population in each epoch.
        """
        num_pops = len(self.epochs[0].populations)
        pop_size = np.zeros((num_pops, len(self.epochs)))
        for j, epoch in enumerate(self.epochs):
            for k, pop in enumerate(epoch.populations):
                pop_size[k, j] = epoch.populations[k].start_size
        return pop_size

    @property
    def epoch_times(self):
        """
        Returns array of epoch times defined by the demographic model
        """
        return np.array([x.start_time for x in self.epochs])

    @property
    def num_epochs(self):
        """
        Returns the number of epochs defined by the demographic model.
        """
        return len(self.epochs)
