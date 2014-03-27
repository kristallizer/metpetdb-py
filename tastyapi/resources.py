from django.db import transaction, IntegrityError
from django.db.models import Q
from django.core.exceptions import ObjectDoesNotExist
from tastypie.resources import ModelResource
from tastypie.constants import ALL, ALL_WITH_RELATIONS
from tastypie import fields
from tastypie.validation import Validation
from tastypie.authorization import Authorization
from tastypie.authentication import ApiKeyAuthentication
from tastypie.exceptions import Unauthorized, InvalidFilterError, ImmediateHttpResponse

from . import models, auth
import logging

logging.basicConfig()
logger = logging.getLogger(__name__)

PREFIX_STRING = '_extra_filter_prefixes'

class BaseResource(ModelResource):
    @transaction.commit_manually
    def dispatch(self, *args, **kwargs):
        # Transaction begins immediately, before we get here
        try:
            response = super(ModelResource, self).dispatch(*args, **kwargs)
            keep = (200 <= response.status_code < 400)
        except:
            transaction.rollback()
            raise
        else:
            if keep:
                transaction.commit()
            else:
                transaction.rollback()
            return response

class VersionedResource(BaseResource):
    def hydrate_version(self, bundle):
        if 'version' in bundle.data:
            bundle.data['version'] += 1
        else:
            bundle.data['version'] = 1
        return bundle

class VersionValidation(Validation):
    def __init__(self, queryset, pk_field):
        self.queryset = queryset
        self.pk_field = pk_field
    def is_valid(self, bundle, request=None):
        if not bundle.data:
            return {'__all__': 'No data.'}
        if self.pk_field in bundle.data:
            try:
                previous = (self.queryset.select_for_update().
                            get(pk=bundle.data[self.pk_field]))
            except ObjectDoesNotExist:
                previous = None
        else:
            previous = None
        if previous is None:
            if request is not None and request.method == 'PUT':
                return {'__all__': 'Cannot find previous version (use POST to create).'}
            elif 'version' in bundle.data and bundle.data['version'] != 0:
                # A version of 0 will be incremented to 1 during hydration
                # Is this needed? Version is already set to 1 by the time we
                # come here.
                # return {'version': 'This should be 0 or absent.'}
                pass
        else:
            if request is not None and request.method == 'POST':
                return {'__all__': 'That object already exists (use PUT to update).'}
            elif 'version' not in bundle.data:
                return {'__all__': 'Data corrupted (version number missing).'}
            elif bundle.data['version'] != previous.version + 1:
                return {'version': 'Edit conflict (object has changed since last GET).'}
        return {}

def _filter_by_permission_closure(permission_lambda):
    """Simple closure to deduplicate code."""
    def closure(self, object_list, bundle):
        """Filter object_list for objects to which we have the given permission."""
        permission = permission_lambda(self)
        result = []
        for item in object_list:
            if bundle.request.user.has_perm(permission, bundle.obj):
                result.append(bundle.obj)
        return result
    return closure

def _check_perm_closure(permission_lambda):
    """Simple closure to deduplicate code."""
    def closure(self, object_list, bundle):
        """Check whether we have permission to access bundle.obj."""
        permission = permission_lambda(self)
        if bundle.request.user.has_perm(permission, bundle.obj):
            return True
        else:
            raise Unauthorized("User {} lacks permission {}."
                               .format(bundle.request.user, permission))
    return closure

class ObjectAuthorization(Authorization):
    """Tastypie custom Authorization class.

    Provides row-level CRUD permissions."""
    def __init__(self, app_name, model_name, *args, **kwargs):
        """Store some information which we'll need later."""
        self.read_perm = "{}.read_{}".format(app_name, model_name)
        self.add_perm = "{}.add_{}".format(app_name, model_name)
        self.change_perm = "{}.change_{}".format(app_name, model_name)
        self.del_perm = "{}.delete_{}".format(app_name, model_name)
        super(ObjectAuthorization, self).__init__(*args, **kwargs)
    def read_list(self, object_list, bundle):
        """Make a queryset of all the objects we can read."""
        filters = auth.get_read_queryset(bundle.request.user)
        qs = object_list.filter(filters)
        return qs
    read_detail = _check_perm_closure(lambda self: self.read_perm)
    def create_list(self, object_list, bundle):
        """Check object_list to make sure new objects are owned by us."""
        logger.info("Received list of objects to create")
        if not bundle.request.user.has_perm(self.add_perm):
            logger.warning("User is not verified.")
            raise Unauthorized("User {} is not manually verified."
                               .format(bundle.request.user))
        result = []
        for item in object_list:
            if item.owner == bundle.request.user:
                result.append(item)
            else:
                logger.warning("User must own created object.")
        return result
    def create_detail(self, object_list, bundle):
        """Check bundle.obj to make sure it is owned by us."""
        user = bundle.request.user
        logger.info("Received single object to create")
        if bundle.obj.user.django_user == user and user.has_perm(self.add_perm):
            return True
        else:
            # print bundle.obj.user.django_user
            logger.warning("User must own created object.")
            raise Unauthorized("User must own created object.")
    update_list = _filter_by_permission_closure(lambda self: self.change_perm)
    update_detail = _check_perm_closure(lambda self: self.change_perm)
    delete_list = _filter_by_permission_closure(lambda self: self.change_perm)
    delete_detail = _check_perm_closure(lambda self: self.change_perm)


class FirstOrderResource(ModelResource):
    """Resource that can only be filtered with "first-order" filters.

    This prevents users from chaining filters through many relationship fields.

    It also applies read permissions to any related objects we filter by, so
    the user cannot deduce information not explicitly available to them.
    """
    def build_filters(self, filters=None):
        """Convert GET parameters into ORM filters.

        This makes a list of all filters which involve related fields and
        attaches that list to the set of ORM filters.  It will then be used
        by apply_filters to
        """
        extra_filter_prefixes = []
        if filters is None:
            filters = {}
        for filter_expr, value in filters.iteritems():
            # For each filter, break it apart by double underscores...
            filter_bits = filter_expr.split("__")
            if len(filter_bits) == 0:
                # can't happen, but fail gracefully anyway
                continue
            # get the first part of the filter
            field_name = filter_bits[0]
            if field_name not in self.fields:
                # It's not a field, abort
                continue
            # Get the field object
            field = self.fields[field_name]
            # Figure out if it's a relationship field
            try:
                is_related = field.is_related
            except AttributeError:
                is_related = False
            if is_related:
                # Field.attribute is the name of the relationship
                extra_filter_prefixes.append(field.attribute)
        result = super(FirstOrderResource, self).build_filters(filters)
        # We can't apply our restrictions at this point, and it would be
        # unwise to attach this to object-wide state since there's only one
        # instance of each resource, so attach it to the result instead.
        # XXX: There's probably a better way to do this.
        result[PREFIX_STRING] = extra_filter_prefixes
        return result
    def apply_filters(self, request, applicable_filters):
        """Applies the given filters.

        Additionally attaches read restrictions to any tables that are joined
        in.  This ensures no information is exposed to the user that they
        aren't explicitly allowed to see.
        """
        auth_read_limit = Q()
        if PREFIX_STRING in applicable_filters:
            # Extract the result we hid during build_filters()
            extra_filter_prefixes = applicable_filters[PREFIX_STRING]
            # Make sure it doesn't interfere with the ORM
            del applicable_filters[PREFIX_STRING]
            for prefix in extra_filter_prefixes:
                # Attach some read limits to the query
                auth_read_limit &= auth.get_read_queryset(request.user, prefix)
        return self.get_object_list(request).filter(auth_read_limit,
                                                    **applicable_filters)
    def check_filtering(self, field_name, filter_type='exact', filter_bits=None):
        """Verify that filter_bits does not begin with a related field lookup.

        This allows the user to make filters that begin with a related lookup,
        but not filters that involve multiple related lookups ("second-order"
        lookups).
        """
        # We want to call the super method right before we return, so wrap it
        # in a lambda expression -- that way we know it's always called right
        result = lambda: (super(FirstOrderResource, self).
                          check_filtering(field_name, filter_type, filter_bits))
        if filter_bits and field_name in self.fields:
            # NB: filter_bits must be non-empty in addition to not being None
            # get the field object
            target_field = self.fields[field_name]
            # Figure out if it's a relationship field
            try:
                is_related = target_field.is_related
            except AttributeError:
                is_related = False
            if not is_related:
                # It isn't, so we don't have to do anything
                return result()
            # get the resource at the "other end" of this relationship
            target_resource = target_field.get_related_resource(None)
            # get the next part of the filter
            next_filter = filter_bits[0]
            if next_filter not in target_resource.fields:
                # the filter doesn't refer to a field on the resource
                return result()
            next_field = target_resource.fields[next_filter]
            try:
                is_second_order = next_field.is_related
            except AttributeError:
                is_second_order = False
            if is_second_order:
                # The filter refers to a relationship field on the resource,
                # so we need to abort since we can't handle those correctly
                raise InvalidFilterError("Second-order relationship traversal"+
                                         " is not allowed.")
        return result()
    def dehydrate(self, bundle):
        """Remove references to inaccessible objects."""
        for field_name in bundle.data:
            # For every field
            if field_name not in self.fields:
                # Don't know what it is, abort
                continue
            # Get the corresponding field object
            target_field = self.fields[field_name]
            # Figure out whether it's a relationship field
            try:
                is_related = target_field.is_related
            except AttributeError:
                # it isn't; abort
                continue
            if not is_related:
                # it isn't; abort
                continue
            # Get the value we're providing for this field
            value = bundle.data[field_name]
            # Many-to-many fields will produce a list here, but a foreign key
            # won't
            if not isinstance(value, list):
                value = [value]
                # NB: Now value is not bundle.data[field_name], so we'll need
                # to re-unify them afterwards
            for item in value[:]:
                try:
                    # Check whether we have permission to access item
                    target_resource = target_field.get_related_resource(None)
                    target_resource.get_via_uri(item, bundle.request)
                except ImmediateHttpResponse:
                    # We don't have permission, remove it
                    value.remove(item)
            if value is not bundle.data[field_name] and not value:
                # For ToOne fields: value was a singleton, and now it's empty,
                # so null out the corresponding entry in bundle.data
                bundle.data[field_name] = None
        return bundle

class UserResource(BaseResource):
    class Meta:
        queryset = models.User.objects.all()
        authorization = Authorization()
        authentication = ApiKeyAuthentication()
        excludes = ['password', 'confirmation_code']

class SampleResource(VersionedResource, FirstOrderResource):
    user = fields.ToOneField("tastyapi.resources.UserResource", "user")
    rock_type = fields.ToOneField("tastyapi.resources.RockTypeResource",
                                  "rock_type")
    regions = fields.ToManyField("tastyapi.resources.RegionResource",
                                 "regions")
    references = fields.ToManyField("tastyapi.resources.ReferenceResource",
                                    "references")
    minerals = fields.ToManyField("tastyapi.resources.MineralResource",
                                  "minerals")
    metamorphic_grades = fields.ToManyField(
                                "tastyapi.resources.MetamorphicGradeResource",
                                "metamorphic_grades")
    metamorphic_regions = fields.ToManyField(
                                "tastyapi.resources.MetamorphicRegionResource",
                                "metamorphic_regions")

    class Meta:
        queryset = models.Sample.objects.all()
        allowed_methods = ['get', 'post', 'put', 'delete']
        authentication = ApiKeyAuthentication()
        authorization = ObjectAuthorization('tastyapi', 'sample')
        excludes = ['user', 'collector']
        filtering = {
                'version': ALL,
                'sesar_number': ALL,
                'public_data': ALL,
                'collection_date': ALL,
                'rock_type': ALL_WITH_RELATIONS,
                'regions': ALL_WITH_RELATIONS,
                }
        validation = VersionValidation(queryset, 'sample_id')

    def save_m2m(self, bundle):
        # Map the M2M request fields to their appropriate classes
        class_mapping = {'regions': models.SampleRegion,
                         'references': models.SampleReference,
                         'minerals': models.SampleMineral,
                         'metamorphic_grades': models.SampleMetamorphicGrade,
                         'metamorphic_regions': models.SampleMetamorphicRegion}

        # Map the request fields to the appropirate field names on the
        # intermediate "through" table
        field_mapping = {'regions': 'region',
                         'references': 'reference',
                         'minerals': 'mineral',
                         'metamorphic_grades': 'metamorphic_grade',
                         'metamorphic_regions': 'metamorphic_region'}

        for field_name, field_object in self.fields.items():
            if not getattr(field_object, 'is_m2m', False):
                continue

            if not field_object.attribute:
                continue

            # This will get or create a new record for each M2M record passed
            # in the request. The class_mapping variable above specifies which
            # classes are currently accepted.
            for field in bundle.data[field_name]:
                try: id = class_mapping[field_name].objects.latest('id').id + 1
                except class_mapping[field_name].DoesNotExist: id = 1

                kwargs = {'id': id,
                          'sample': models.Sample.objects.get(
                                                      pk=bundle.obj.sample_id),
                          field_mapping[field_name]: field.obj}

                try: class_mapping[field_name].objects.get_or_create(**kwargs)
                except IntegrityError: continue


class RegionResource(BaseResource):
    class Meta:
        queryset = models.Region.objects.all()
        authentication = ApiKeyAuthentication()
        allowed_methods = ['get']
        resource_name = "region"
        filtering = { 'region': ALL }

class RockTypeResource(BaseResource):
    samples = fields.ToManyField(SampleResource, "sample_set")
    class Meta:
        resource_name = "rock_type"
        authentication = ApiKeyAuthentication()
        queryset = models.RockType.objects.all()
        allowed_methods = ['get', 'post', 'put']
        filtering = { 'rock_type': ALL }

class MineralResource(BaseResource):
    real_mineral = fields.ToOneField('tastyapi.resources.MineralResource',
                                     'real_mineral')
    # analyses = fields.ToManyField('tastyapi.resources.ChemicalAnalysisResource',
    #                               'chemicalanalysis_set')
    class Meta:
        resource_name = 'mineral'
        queryset = models.Mineral.objects.all()
        authentication = ApiKeyAuthentication()
        allowed_methods = ['get']
        filtering = {
                'name': ALL,
                'real_mineral': ALL_WITH_RELATIONS,
                }

class MetamorphicGradeResource(BaseResource):
    class Meta:
        resource_name = "metamorphic_grade"
        authentication = ApiKeyAuthentication()
        queryset = models.MetamorphicGrade.objects.all()
        allowed_methods = ['get']
        filtering = { 'name': ALL }

class MetamorphicRegionResource(BaseResource):
    class Meta:
        resource_name = "metamorphic_region"
        authentication = ApiKeyAuthentication()
        queryset = models.MetamorphicRegion.objects.all()
        allowed_methods = ['get']
        filtering = { 'name': ALL }

class SubsampleTypeResource(BaseResource):
    subsamples = fields.ToManyField("tastyapi.resources.SubsampleResource",
                                 "subsample_set")
    class Meta:
        resource_name = 'subsample_type'
        queryset = models.SubsampleType.objects.all()
        authentication = ApiKeyAuthentication()
        filtering = {'subsample_type': ALL}

class SubsampleResource(VersionedResource, FirstOrderResource):
    sample = fields.ToOneField(SampleResource, "sample")
    subsample_type = fields.ToOneField(SubsampleTypeResource, "subsample_type")
    class Meta:
        queryset = models.Subsample.objects.all()
        excludes = ['user']
        authorization = ObjectAuthorization('tastyapi', 'subsample')
        authentication = ApiKeyAuthentication()
        # authorization = Authorization()
        filtering = {
                'public_data': ALL,
                'grid_id': ALL,
                'name': ALL,
                'subsample_type': ALL_WITH_RELATIONS,
                }
        validation = VersionValidation(queryset, 'subsample_id')




class ReferenceResource(BaseResource):
    # analyses = fields.ToManyField('tastyapi.resources.ChemicalAnalysisResource',
    #                                 'chemicalanalysis_set')
    class Meta:
        queryset = models.Reference.objects.all()
        allowed_methods = ['get']
        authentication = ApiKeyAuthentication()
        filtering = {'name': ALL}

class ChemicalAnalysisResource(VersionedResource, FirstOrderResource):
    subsample = fields.ToOneField(SubsampleResource, "subsample")
    reference = fields.ToOneField(ReferenceResource, "reference", null=True)
    mineral = fields.ToOneField(MineralResource, "mineral", null=True)
    class Meta:
        resource_name = 'chemical_analysis'
        queryset = models.ChemicalAnalyses.objects.all()
        excludes = ['image', 'user']
        authorization = ObjectAuthorization('tastyapi', 'chemical_analysis')
        authentication = ApiKeyAuthentication()
        filtering = {
                'subsample': ALL_WITH_RELATIONS,
                'reference': ALL_WITH_RELATIONS,
                'public_data': ALL,
                'reference_x': ALL,
                'reference_y': ALL,
                'stage_x': ALL,
                'stage_y': ALL,
                'analysis_method': ALL,
                'where_done': ALL,
                'analyst': ALL,
                'analysis_date': ALL,
                'large_rock': ALL,
                'total': ALL,
                }
        validation = VersionValidation(queryset, 'chemical_analysis_id')
