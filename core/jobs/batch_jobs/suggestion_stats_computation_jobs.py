# coding: utf-8
#
# Copyright 2021 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Jobs that are run by CRON scheduler."""

from __future__ import annotations

import datetime

from core import feconf
from core.domain import html_cleaner
from core.domain import opportunity_domain
from core.domain import opportunity_services
from core.domain import suggestion_registry
from core.domain import suggestion_services
from core.jobs import base_jobs
from core.jobs.io import ndb_io
from core.jobs.types import job_run_result
from core.platform import models

import apache_beam as beam

from typing import Dict, Iterable, Optional, Tuple, Union

MYPY = False
if MYPY: # pragma: no cover
    from mypy_imports import datastore_services
    from mypy_imports import opportunity_models
    from mypy_imports import suggestion_models

(opportunity_models, suggestion_models) = models.Registry.import_models(
    [models.NAMES.opportunity, models.NAMES.suggestion])

datastore_services = models.Registry.import_datastore_services()


class GenerateTranslationContributionStatsJob(base_jobs.JobBase):
    """Job that indexes the explorations in Elastic Search."""

    def run(self) -> beam.PCollection[job_run_result.JobRunResult]:
        """Generates the translation contributins stats.

        Returns:
            PCollection. A PCollection of 'SUCCESS x' results, where x is
            the number of generated stats..
        """
        suggestions_grouped_by_target = (
            self.pipeline
            | 'Get all non-deleted suggestion models' >> ndb_io.GetModels(
                suggestion_models.GeneralSuggestionModel.get_all(
                    include_deleted=False))
            # We need to window the models so that CoGroupByKey below
            # works properly.
            | 'Filter translate suggestions' >> beam.Filter(
                lambda m: (
                    m.suggestion_type ==
                    feconf.SUGGESTION_TYPE_TRANSLATE_CONTENT
                ))
            | 'Transform to suggestion domain object' >> beam.Map(
                suggestion_services.get_suggestion_from_model)
            | 'Group by target' >> beam.GroupBy(lambda m: m.target_id)
        )
        exp_opportunities = (
            self.pipeline
            | 'Get all non-deleted opportunity models' >> ndb_io.GetModels(
                opportunity_models.ExplorationOpportunitySummaryModel.get_all(
                    include_deleted=False))
            # We need to window the models so that CoGroupByKey below
            # works properly.
            | 'Transform to opportunity domain object' >> beam.Map(
                opportunity_services.
                get_exploration_opportunity_summary_from_model)
            | 'Group by ID' >> beam.GroupBy(lambda m: m.id)
        )

        new_user_stats_models = (
            {
                'suggestion': suggestions_grouped_by_target,
                'opportunity': exp_opportunities
            }
            | 'Merge models' >> beam.CoGroupByKey()
            | 'Get rid of key' >> beam.Values()  # pylint: disable=no-value-for-parameter
            | 'Generate stats' >> beam.ParDo(
                lambda x: self._generate_stats(
                    x['suggestion'][0] if len(x['suggestion']) else [],
                    list(x['opportunity'][0])[0]
                    if len(x['opportunity']) else None
                ))
            | 'Combine the stats' >> beam.CombinePerKey(CombineStats())
            | 'Generate models from stats' >> beam.MapTuple(
                self._generate_translation_contribution_model)
        )

        unused_put_result = (
            new_user_stats_models
            | 'Put models into the datastore' >> ndb_io.PutModels()
        )

        return (
            new_user_stats_models
            | 'Count all new models' >> (
                beam.combiners.Count.Globally().without_defaults())
            | 'Only create result for new models when > 0' >> (
                beam.Filter(lambda x: x > 0))
            | 'Create result for new models' >> beam.Map(
                lambda x: job_run_result.JobRunResult(
                    stdout='SUCCESS %s' % x)
                )
        )

    @staticmethod
    def _generate_stats(
        suggestions: Iterable[suggestion_registry.SuggestionTranslateContent],
        opportunity: Optional[opportunity_domain.ExplorationOpportunitySummary]
    ) -> Iterable[Tuple[str, Dict[str, Union[bool, int, str]]]]:
        """Generates translation contribution stats for each suggestion.

        Args:
            suggestions: iter(SuggestionTranslateContent). Suggestions for which
                the stats should be generated.
            opportunity: ExplorationOpportunitySummary. Opportunity for which
                were the suggestions generated. Used to extract topic ID.

        Yields:
            tuple(str, Dict(str, *)). Tuple of key and suggestion stats dict.
            The stats dictionary has four fields:
                suggestion_status: str. What is the status of the suggestion.
                edited_by_reviewer: bool. Whether the suggestion was edited by
                    the reviewer.
                content_word_count: int. The word count of the content of
                    the suggestion.
                last_updated_date: str. When was the suggestion last updated.
        """
        # When opportunity is not available we leave the topic ID empty.
        topic_id = ''
        if opportunity is not None:
            topic_id = opportunity.topic_id

        for suggestion in suggestions:
            # Count the number of words in the original content, ignoring any
            # HTML tags and attributes.
            content_plain_text = html_cleaner.strip_html_tags( # type: ignore[no-untyped-call]
                suggestion.change.content_html) # type: ignore[attr-defined]
            content_word_count = len(content_plain_text.split())

            key = (
                suggestion_models.TranslationContributionStatsModel.generate_id(
                    suggestion.language_code, suggestion.author_id, topic_id))
            translation_contribution_stats_dict = {
                'suggestion_status': suggestion.status,
                'edited_by_reviewer': suggestion.edited_by_reviewer,
                'content_word_count': content_word_count,
                'last_updated_date': suggestion.last_updated.date().isoformat()
            }
            yield (key, translation_contribution_stats_dict)

    @staticmethod
    def _generate_translation_contribution_model(
        entity_id: str,
        translation: suggestion_registry.TranslationContributionStats
    ) -> suggestion_models.TranslationContributionStatsModel:
        """Generate translation contribution stats model from the domain object.

        Args:
            entity_id: str. The ID of the model.
            translation: TranslationContributionStats. Domain object.

        Returns:
            TranslationContributionStatsModel. The created model.
        """
        language_code, contributor_user_id, topic_id = entity_id.split('.')
        with datastore_services.get_ndb_context():
            translation_contributions_stats_model = (
                suggestion_models.TranslationContributionStatsModel(
                    id=entity_id,
                    language_code=language_code,
                    contributor_user_id=contributor_user_id,
                    topic_id=topic_id,
                    submitted_translations_count=(
                        translation.submitted_translations_count),
                    submitted_translation_word_count=(
                        translation.submitted_translation_word_count),
                    accepted_translations_count=(
                        translation.accepted_translations_count),
                    accepted_translations_without_reviewer_edits_count=(
                        translation
                        .accepted_translations_without_reviewer_edits_count
                    ),
                    accepted_translation_word_count=(
                        translation.accepted_translation_word_count),
                    rejected_translations_count=(
                        translation.rejected_translations_count),
                    rejected_translation_word_count=(
                        translation.rejected_translation_word_count),
                    contribution_dates=translation.contribution_dates
                )
            )
            translation_contributions_stats_model.update_timestamps()
            return translation_contributions_stats_model


class CombineStats(beam.CombineFn):  # type: ignore[misc]
    """CombineFn for combining the stats."""

    def create_accumulator(
        self
    ) -> suggestion_registry.TranslationContributionStats:
        return suggestion_registry.TranslationContributionStats.create_default()

    def add_input(
        self,
        accumulator: suggestion_registry.TranslationContributionStats,
        translation: Dict[str, Union[bool, int, str]]
    ) -> suggestion_registry.TranslationContributionStats:
        is_accepted = (
            translation['suggestion_status'] ==
            suggestion_models.STATUS_ACCEPTED
        )
        is_accepted_and_not_edited = (
            is_accepted and not translation['edited_by_reviewer'])
        is_rejected = (
            translation['suggestion_status'] ==
            suggestion_models.STATUS_REJECTED
        )
        word_count = translation['content_word_count']
        suggestion_date = datetime.datetime.strptime(
            str(translation['last_updated_date']), '%Y-%m-%d').date()
        return suggestion_registry.TranslationContributionStats( # type: ignore[no-untyped-call]
            accumulator.language_code,
            accumulator.contributor_user_id,
            accumulator.topic_id,
            accumulator.submitted_translations_count + 1,
            accumulator.submitted_translation_word_count + word_count,
            accumulator.accepted_translations_count + int(is_accepted),
            (
                accumulator.accepted_translations_without_reviewer_edits_count +
                int(is_accepted_and_not_edited)
            ),
            (
                accumulator.accepted_translation_word_count +
                word_count * int(is_accepted)
            ),
            accumulator.rejected_translations_count + int(is_rejected),
            (
                accumulator.rejected_translation_word_count +
                word_count * int(is_rejected)
            ),
            accumulator.contribution_dates | {suggestion_date}
        )

    def merge_accumulators(
        self,
        accumulators: Iterable[suggestion_registry.TranslationContributionStats]
    ) -> suggestion_registry.TranslationContributionStats:
        return suggestion_registry.TranslationContributionStats( # type: ignore[no-untyped-call]
            list(accumulators)[0].language_code,
            list(accumulators)[0].contributor_user_id,
            list(accumulators)[0].topic_id,
            sum(acc.submitted_translations_count for acc in accumulators),
            sum(acc.submitted_translation_word_count for acc in accumulators),
            sum(acc.accepted_translations_count for acc in accumulators),
            sum(
                acc.accepted_translations_without_reviewer_edits_count
                for acc in accumulators
            ),
            sum(acc.accepted_translation_word_count for acc in accumulators),
            sum(acc.rejected_translations_count for acc in accumulators),
            sum(acc.rejected_translation_word_count for acc in accumulators),
            set().union(*[acc.contribution_dates for acc in accumulators])
        )

    def extract_output(
        self, accumulator: suggestion_registry.TranslationContributionStats
    ) -> suggestion_registry.TranslationContributionStats:
        return accumulator
