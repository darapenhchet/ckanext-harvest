import re
import logging
import uuid

import pylons

from ckan import model
from ckan import logic
from ckan.lib.navl.validators import not_empty
from ckan.lib.munge import munge_title_to_name
from ckan.lib.helpers import json
from ckan.plugins import toolkit as tk
from ckanext.harvest.model import (HarvestObject, HarvestGatherError,
                                   HarvestObjectError)
from ckanext.harvest.harvesters.base import (HarvesterBase, munge_tags,
                                             remove_duplicates_in_a_list)


class DguHarvesterBase(HarvesterBase):
    '''
    Data.gov.uk's customized base class for harvesters, providing them with a
    common import_stage and various helper functions. A harvester doesn't have
    to derive from this - it should just have:

        implements(IHarvester)

    however this base class avoids lots of work dealing with HarvestObjects.
    '''
    @staticmethod
    def _munge_title_to_name(title):
        '''
        Creates a URL friendly name from a title. Compared to the ckan method
        munge_title_to_name, this version also collapses multiple dashes into
        single ones.
        '''
        name = munge_title_to_name(title)
        name = re.sub('-+', '-', name)  # collapse multiple dashes
        return name
    munge_title_to_name = _munge_title_to_name

    @staticmethod
    def extras_from_dict(extras_dict):
        '''Takes extras in the form of a dict and returns it in the form of a
        list of dicts.  A single extras dict is convenient to fill with values
        in get_package_dict() but it needs to return the extras as a list of
        dicts, to suit package_update.

        e.g.
        >>> HarvesterBase.extras_from_dict({'theme': 'environment', 'freq': 'daily'})
        [{'key': 'theme', 'value': 'environment'}, {'key': 'freq', 'value': 'daily'}]
        '''
        return [{'key': key, 'value': value} for key, value in extras_dict.items()]

    def import_stage(self, harvest_object):
        '''The import_stage contains lots of boiler plate, updating the
        harvest_objects correctly etc, so inherit this method and customize the
        get_package_dict method.

        * HOExtra.status should have been set to 'new', 'changed' or 'deleted'
          in the gather or fetch stages.
        * It follows that checking that the metadata date has changed should
          have been done in the gather or fetch stages
        * harvest_object.source.config can control default additions to the
          package, for extras etc
        '''

        log = logging.getLogger(__name__ + '.import')
        log.debug('Import stage for harvest object: %s', harvest_object.id)

        if not harvest_object:
            # something has gone wrong with the code
            log.error('No harvest object received')
            self._save_object_error('System error')
            return False
        if harvest_object.content is None:
            # fetched object is blank - error with the harvested server
            self._save_object_error('Empty content for object %s' %
                                    harvest_object.id,
                                    harvest_object, 'Import')
            return False

        source_config = json.loads(harvest_object.source.config or '{}')

        status = harvest_object.get_extra('status')
        if not status in ['new', 'changed', 'deleted']:
            log.error('Status is not set correctly: %r', status)
            self._save_object_error('System error', harvest_object, 'Import')
            return False

        # Get the last harvested object (if any)
        previous_object = \
            model.Session.query(HarvestObject) \
                 .filter(HarvestObject.guid == harvest_object.guid) \
                 .filter(HarvestObject.current == True) \
                 .first()

        user = source_config.get('user', 'harvest')

        context = {'model': model, 'session': model.Session, 'user': user,
                   'api_version': 3, 'extras_as_string': True}

        if status == 'delete':
            # Delete package
            tk.get_action('package_delete')(context, {'id': harvest_object.package_id})
            log.info('Deleted package {0} with guid {1}'.format(harvest_object.package_id, harvest_object.guid))
            previous_object.save()
            self._transfer_current(previous_object, harvest_object)
            return True

        # Set defaults for the package_dict, mainly from the source_config
        package_dict_defaults = PackageDictDefaults()
        package_id = previous_object.package_id if previous_object else None
        package_dict_defaults['id'] = package_id or unicode(uuid.uuid4())
        existing_dataset = model.Package.get(package_id)

        if existing_dataset:
            package_dict_defaults['name'] = existing_dataset.name
        if source_config.get('remote_orgs') not in ('only_local', 'create'):
            # Assign owner_org to the harvest_source's publisher
            #master would get the harvest_object.source.publisher_id this way:
            #source_dataset = tk.get_action('package_show')(context, {'id': harvest_object.source.id})
            #local_org = source_dataset.get('owner_org')
            package_dict_defaults['owner_org'] = harvest_object.source.publisher_id
        elif existing_dataset and existing_dataset.owner_org:
            package_dict_defaults['owner_org'] = existing_dataset.owner_org
        package_dict_defaults['tags'] = source_config.get('default_tags', [])
        package_dict_defaults['groups'] = source_config.get('default_groups', [])
        package_dict_defaults['extras'] = {
            'import_source': 'harvest',  # to identify all harvested datasets
            'harvest_object_id': harvest_object.id,
            'guid': harvest_object.guid,
            'metadata-date': harvest_object.metadata_modified_date.strftime('%Y-%m-%d')
                             if harvest_object.metadata_modified_date else None,
            # Add provenance for this harvest, so at least that info is saved
            # even if the harvester doesn't fill it in properly with get_provenance().
            'metadata_provenance': self.get_metadata_provenance(harvest_object, harvested_provenance=None),
            }
        default_extras = source_config.get('default_extras', {})
        if default_extras:
            env = dict(harvest_source_id=harvest_object.job.source.id,
                       harvest_source_url=harvest_object.job.source.url.strip('/'),
                       harvest_source_title=harvest_object.job.source.title,
                       harvest_job_id=harvest_object.job.id,
                       harvest_object_id=harvest_object.id,
                       dataset_id=package_dict_defaults['id'])
            for key, value in default_extras.iteritems():
                # Look for replacement strings
                if isinstance(value, basestring):
                    value = value.format(env)
                package_dict_defaults['extras'][key] = value
        if existing_dataset:
            extras_kept = set(
                pylons.config.get('ckan.harvest.extras_not_overwritten', '')
                .split(' '))
            for extra_key in extras_kept:
                if extra_key in existing_dataset.extras:
                    package_dict_defaults['extras'][extra_key] = \
                        existing_dataset.extras.get(extra_key)

        if status in ('new', 'changed'):
            # There are 2 circumstances that the status is wrong:
            # 1. we are using 'paster import' to reimport this object, yet
            # status is still 'new' from the previous harvest, yet it needs to
            # be 'changed' so that it does a package_update().
            # 2. the first harvest excepted, so status is 'new' because the
            # harvest_object is there, but no package was created.
            # Simplest solution is to set it according to whether there is an
            # existing dataset.
            status = 'changed' if existing_dataset else 'new'
            harvest_object.set_extra('status', status)
            harvest_object.save()

        try:
            package_dict = self.get_package_dict(harvest_object,
                                                 package_dict_defaults,
                                                 source_config,
                                                 existing_dataset)
        except PackageDictError, e:
            log.error('Harvest PackageDictError in get_package_dict %s %r',
                      e, harvest_object)
            self._save_object_error('Error converting to dataset: %s' % e,
                                    harvest_object, 'Import')
            return False
        except Exception, e:
            log.exception('Harvest error in get_package_dict %r', harvest_object)
            self._save_object_error('System error', harvest_object, 'Import')
            return False
        if not package_dict:
            # Nothing to harvest after all.
            # No error should be recorded, so that's why we return True.
            # Yet this object is not 'current', so it's clear we skipped.
            return True

        if source_config.get('clean_tags'):
            munge_tags(package_dict)

        if status == 'changed':
            self._match_resources_with_existing_ones(
                package_dict['resources'],
                existing_dataset.resources)

        # Create or update the package object

        if status == 'new':
            package_schema = logic.schema.default_create_package_schema()
        else:
            package_schema = logic.schema.default_update_package_schema()

        # Drop the validation restrictions on tags
        # (TODO: make this optional? get schema from config?)
        tag_schema = logic.schema.default_tags_schema()
        tag_schema['name'] = [not_empty, unicode]
        package_schema['tags'] = tag_schema
        context['schema'] = package_schema

        if status == 'new':
            # We need to explicitly provide a package ID, otherwise
            # ckanext-spatial won't be be able to link the extent to the
            # package.
            if not package_dict.get('id'):
                package_dict['id'] = unicode(uuid.uuid4())
            package_schema['id'] = [unicode]

            # Save reference to the package on the object
            harvest_object.package_id = package_dict['id']
            # Defer constraints and flush so the dataset can be indexed with
            # the harvest object id (on the after_show hook from the harvester
            # plugin)
            if model.engine_is_pg():
                model.Session.execute('SET CONSTRAINTS harvest_object_package_id_fkey DEFERRED')
                model.Session.flush()

            if source_config.get('private_datasets', False):
                package_dict['private'] = True

            log.debug('package_create: %r', package_dict)
            try:
                package_dict_created = tk.get_action('package_create')(context, package_dict)
                log.info('Created new package name=%s id=%s guid=%s', package_dict.get('name'), package_dict_created['id'], harvest_object.guid)
            except tk.ValidationError, e:
                self._save_object_error('Validation Error: %s' % str(e.error_summary), harvest_object, 'Import')
                return False
        elif status == 'changed':
            package_schema = logic.schema.default_update_package_schema()
            package_dict['id'] = package_id
            log.debug('package_update: %r', package_dict)
            try:
                package_dict_updated = tk.get_action('package_update')(context, package_dict)
                log.info('Updated package name=%s id=%s guid=%s', package_dict.get('name'), package_dict_updated['id'], harvest_object.guid)
            except tk.ValidationError, e:
                self._save_object_error('Validation Error: %s' % str(e.error_summary), harvest_object, 'Import')
                return False

        # Successful import
        self._transfer_current(previous_object, harvest_object)
        return True

    def get_package_dict(self, harvest_object, package_dict_defaults,
                         source_config, existing_dataset):
        '''
        Constructs a package_dict suitable to be passed to package_create or
        package_update. See documentation on
        ckan.logic.action.create.package_create for more details

        * name - a new package must have a unique name; if it had a name in the
          previous harvest, that will be in the package_dict_defaults.
        * resource.id - should be the same as the old object if updating a
          package
        * errors - call self._save_object_error() and return False
        * default values for name, owner_org, tags etc can be merged in using:
            package_dict = package_dict_defaults.merge(package_dict_harvested)
            (NB default extras should be a dict, not a list of dicts)
        * extras should be converted from a dict to a list of dicts before
          returning - use extras_from_dict()

        On error, raise PackageDictError() which will record the error and
        cancel the import of the object gracefully.

        If there is nothing to import, then return None and no error will be
        recorded.

        :param harvest_object: HarvestObject domain object (with access to
                               job and source objects)
        :type harvest_object: HarvestObject
        :param package_dict_defaults: Suggested/default values for the
          package_dict, based on the config, a previously harvested object, etc
        :type package_dict_defaults: dict
        :param source_config: The config of the harvest source
        :type source_config: dict
        :param source_config: The dataset as it was harvested last time. Needed
          to set resource IDs the same as with existing resources.
        :type existing_dataset: Package

        :returns: A dataset dictionary (package_dict)
        :rtype: dict
        '''
        pass


class PackageDictError(Exception):
    pass

class PackageDictDefaults(dict):
    def merge(self, package_dict):
        '''
        Returns a dict based on the passed-in package_dict and adding default
        values from self. Where the key is a string, the default is a
        fall-back for a blank value in the package_dict. Where the key is a
        list or dict, the values are merged.
        '''
        merged = package_dict.copy()
        for key in self:
            try:
                if isinstance(self[key], list):
                    merged[key] = self[key] + merged.get(key, [])
                    merged[key] = remove_duplicates_in_a_list(merged[key])
                elif isinstance(self[key], dict):
                    merged[key] = dict(self[key].items() +
                                        merged.get(key, {}).items())
                elif isinstance(self[key], basestring):
                    merged[key] = merged.get(key) or self[key]
                else:
                    raise NotImplementedError()
            except Exception, e:
                # Raise a better exception with more info
                import sys
                raise type(e), type(e)(e.message + ' (key=%s)' % key), \
                      sys.exc_info()[2]
        return merged
