# -*- coding: utf-8 -*-
"""
Programmatic integration point for User API Accounts sub-application
"""
from django.utils.translation import override as override_language, ugettext as _
from django.db import transaction, IntegrityError
import datetime
from pytz import UTC
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from django.core.validators import validate_email, ValidationError
from django.http import HttpResponseForbidden
from openedx.core.djangoapps.user_api.preferences.api import update_user_preferences
from openedx.core.djangoapps.user_api.errors import PreferenceValidationError

from student.models import User, UserProfile, Registration
from student import forms as student_forms
from student import views as student_views
from util.model_utils import emit_setting_changed_event

from openedx.core.lib.api.view_utils import add_serializer_errors

from ..errors import (
    AccountUpdateError, AccountValidationError,
    AccountDataBadLength, AccountDataBadType,
    AccountUsernameInvalid, AccountPasswordInvalid, AccountEmailInvalid,
    AccountUserAlreadyExists, AccountUsernameAlreadyExists, AccountEmailAlreadyExists,
    UserAPIInternalError, UserAPIRequestError, UserNotFound, UserNotAuthorized
)
from ..forms import PasswordResetFormNoActive
from ..helpers import intercept_errors

from . import (
    EMAIL_BAD_LENGTH_MSG, PASSWORD_BAD_LENGTH_MSG, USERNAME_BAD_LENGTH_MSG,
    EMAIL_BAD_TYPE_MSG, PASSWORD_BAD_TYPE_MSG, USERNAME_BAD_TYPE_MSG,
    EMAIL_CONFLICT_MSG, USERNAME_CONFLICT_MSG,
    EMAIL_INVALID_MSG,
    EMAIL_MIN_LENGTH, PASSWORD_MIN_LENGTH, USERNAME_MIN_LENGTH,
    EMAIL_MAX_LENGTH, PASSWORD_MAX_LENGTH, USERNAME_MAX_LENGTH,
    PASSWORD_CANT_EQUAL_USERNAME_MSG
)
from .serializers import (
    AccountLegacyProfileSerializer, AccountUserSerializer,
    UserReadOnlySerializer, _visible_fields  # pylint: disable=invalid-name
)
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers


# Public access point for this function.
visible_fields = _visible_fields


@intercept_errors(UserAPIInternalError, ignore_errors=[UserAPIRequestError])
def get_account_settings(request, usernames=None, configuration=None, view=None):
    """Returns account information for a user serialized as JSON.

    Note:
        If `request.user.username` != `username`, this method will return differing amounts of information
        based on who `request.user` is and the privacy settings of the user associated with `username`.

    Args:
        request (Request): The request object with account information about the requesting user.
            Only the user with username `username` or users with "is_staff" privileges can get full
            account information. Other users will get the account fields that the user has elected to share.
        usernames (list): Optional list of usernames for the desired account information. If not
            specified, `request.user.username` is assumed.
        configuration (dict): an optional configuration specifying which fields in the account
            can be shared, and the default visibility settings. If not present, the setting value with
            key ACCOUNT_VISIBILITY_CONFIGURATION is used.
        view (str): An optional string allowing "is_staff" users and users requesting their own
            account information to get just the fields that are shared with everyone. If view is
            "shared", only shared account information will be returned, regardless of `request.user`.

    Returns:
         A list of users account details.

    Raises:
         UserNotFound: no user with username `username` exists (or `request.user.username` if
            `username` is not specified)
         UserAPIInternalError: the operation failed due to an unexpected error.

    """
    requesting_user = request.user
    usernames = usernames or [requesting_user.username]

    requested_users = User.objects.select_related('profile').filter(username__in=usernames)
    if not requested_users:
        raise UserNotFound()

    serialized_users = []
    for user in requested_users:
        has_full_access = requesting_user.is_staff or requesting_user.username == user.username
        if has_full_access and view != 'shared':
            admin_fields = settings.ACCOUNT_VISIBILITY_CONFIGURATION.get('admin_fields')
        else:
            admin_fields = None
        serialized_users.append(UserReadOnlySerializer(
            user,
            configuration=configuration,
            custom_fields=admin_fields,
            context={'request': request}
        ).data)

    return serialized_users


@intercept_errors(UserAPIInternalError, ignore_errors=[UserAPIRequestError])
def update_account_settings(requesting_user, update, username=None):
    """Update user account information.

    Note:
        It is up to the caller of this method to enforce the contract that this method is only called
        with the user who made the request.

    Arguments:
        requesting_user (User): The user requesting to modify account information. Only the user with username
            'username' has permissions to modify account information.
        update (dict): The updated account field values.
        username (str): Optional username specifying which account should be updated. If not specified,
            `requesting_user.username` is assumed.

    Raises:
        UserNotFound: no user with username `username` exists (or `requesting_user.username` if
            `username` is not specified)
        UserNotAuthorized: the requesting_user does not have access to change the account
            associated with `username`
        AccountValidationError: the update was not attempted because validation errors were found with
            the supplied update
        AccountUpdateError: the update could not be completed. Note that if multiple fields are updated at the same
            time, some parts of the update may have been successful, even if an AccountUpdateError is returned;
            in particular, the user account (not including e-mail address) may have successfully been updated,
            but then the e-mail change request, which is processed last, may throw an error.
        UserAPIInternalError: the operation failed due to an unexpected error.

    """
    if username is None:
        username = requesting_user.username

    existing_user, existing_user_profile = _get_user_and_profile(username)

    if requesting_user.username != username:
        raise UserNotAuthorized()

    # If user has requested to change email, we must call the multi-step process to handle this.
    # It is not handled by the serializer (which considers email to be read-only).
    changing_email = False
    if "email" in update:
        changing_email = True
        new_email = update["email"]
        del update["email"]

    # If user has requested to change name, store old name because we must update associated metadata
    # after the save process is complete.
    old_name = None
    if "name" in update:
        old_name = existing_user_profile.name

    # Check for fields that are not editable. Marking them read-only causes them to be ignored, but we wish to 400.
    read_only_fields = set(update.keys()).intersection(
        AccountUserSerializer.get_read_only_fields() + AccountLegacyProfileSerializer.get_read_only_fields()
    )

    # Build up all field errors, whether read-only, validation, or email errors.
    field_errors = {}

    if read_only_fields:
        for read_only_field in read_only_fields:
            field_errors[read_only_field] = {
                "developer_message": u"This field is not editable via this API",
                "user_message": _(u"The '{field_name}' field cannot be edited.").format(field_name=read_only_field)
            }
            del update[read_only_field]

    user_serializer = AccountUserSerializer(existing_user, data=update)
    legacy_profile_serializer = AccountLegacyProfileSerializer(existing_user_profile, data=update)

    for serializer in user_serializer, legacy_profile_serializer:
        field_errors = add_serializer_errors(serializer, update, field_errors)

    # If the user asked to change email, validate it.
    if changing_email:
        try:
            student_views.validate_new_email(existing_user, new_email)
        except ValueError as err:
            field_errors["email"] = {
                "developer_message": u"Error thrown from validate_new_email: '{}'".format(err.message),
                "user_message": err.message
            }

    # If we have encountered any validation errors, return them to the user.
    if field_errors:
        raise AccountValidationError(field_errors)

    try:
        # If everything validated, go ahead and save the serializers.

        # We have not found a way using signals to get the language proficiency changes (grouped by user).
        # As a workaround, store old and new values here and emit them after save is complete.
        if "language_proficiencies" in update:
            old_language_proficiencies = list(existing_user_profile.language_proficiencies.values('code'))

        for serializer in user_serializer, legacy_profile_serializer:
            serializer.save()

        # if any exception is raised for user preference (i.e. account_privacy), the entire transaction for user account
        # patch is rolled back and the data is not saved
        if 'account_privacy' in update:
            update_user_preferences(
                requesting_user, {'account_privacy': update["account_privacy"]}, existing_user
            )

        if "language_proficiencies" in update:
            new_language_proficiencies = update["language_proficiencies"]
            emit_setting_changed_event(
                user=existing_user,
                db_table=existing_user_profile.language_proficiencies.model._meta.db_table,
                setting_name="language_proficiencies",
                old_value=old_language_proficiencies,
                new_value=new_language_proficiencies,
            )

        # If the name was changed, store information about the change operation. This is outside of the
        # serializer so that we can store who requested the change.
        if old_name:
            meta = existing_user_profile.get_meta()
            if 'old_names' not in meta:
                meta['old_names'] = []
            meta['old_names'].append([
                old_name,
                u"Name change requested through account API by {0}".format(requesting_user.username),
                datetime.datetime.now(UTC).isoformat()
            ])
            existing_user_profile.set_meta(meta)
            existing_user_profile.save()

    except PreferenceValidationError as err:
        raise AccountValidationError(err.preference_errors)
    except Exception as err:
        raise AccountUpdateError(
            u"Error thrown when saving account updates: '{}'".format(err.message)
        )

    # And try to send the email change request if necessary.
    if changing_email:
        if not settings.FEATURES['ALLOW_EMAIL_ADDRESS_CHANGE']:
            raise AccountUpdateError(u"Email address changes have been disabled by the site operators.")
        try:
            student_views.do_email_change_request(existing_user, new_email)
        except ValueError as err:
            raise AccountUpdateError(
                u"Error thrown from do_email_change_request: '{}'".format(err.message),
                user_message=err.message
            )


@intercept_errors(UserAPIInternalError, ignore_errors=[UserAPIRequestError])
@transaction.atomic
def create_account(username, password, email):
    """Create a new user account.

    This will implicitly create an empty profile for the user.

    WARNING: This function does NOT yet implement all the features
    in `student/views.py`.  Until it does, please use this method
    ONLY for tests of the account API, not in production code.
    In particular, these are currently missing:

    * 3rd party auth
    * External auth (shibboleth)
    * Complex password policies (ENFORCE_PASSWORD_POLICY)

    In addition, we assume that some functionality is handled
    at higher layers:

    * Analytics events
    * Activation email
    * Terms of service / honor code checking
    * Recording demographic info (use profile API)
    * Auto-enrollment in courses (if invited via instructor dash)

    Args:
        username (unicode): The username for the new account.
        password (unicode): The user's password.
        email (unicode): The email address associated with the account.

    Returns:
        unicode: an activation key for the account.

    Raises:
        AccountUserAlreadyExists
        AccountUsernameInvalid
        AccountEmailInvalid
        AccountPasswordInvalid
        UserAPIInternalError: the operation failed due to an unexpected error.

    """
    # Check if ALLOW_PUBLIC_ACCOUNT_CREATION flag turned off to restrict user account creation
    if not configuration_helpers.get_value(
            'ALLOW_PUBLIC_ACCOUNT_CREATION',
            settings.FEATURES.get('ALLOW_PUBLIC_ACCOUNT_CREATION', True)
    ):
        return HttpResponseForbidden(_("Account creation not allowed."))

    # Validate the username, password, and email
    # This will raise an exception if any of these are not in a valid format.
    _validate_username(username)
    _validate_password(password, username)
    _validate_email(email)

    # Create the user account, setting them to "inactive" until they activate their account.
    user = User(username=username, email=email, is_active=False)
    user.set_password(password)

    try:
        user.save()
    except IntegrityError:
        raise AccountUserAlreadyExists

    # Create a registration to track the activation process
    # This implicitly saves the registration.
    registration = Registration()
    registration.register(user)

    # Create an empty user profile with default values
    UserProfile(user=user).save()

    # Return the activation key, which the caller should send to the user
    return registration.activation_key


def check_account_exists(username=None, email=None):
    """Check whether an account with a particular username or email already exists.

    Keyword Arguments:
        username (unicode)
        email (unicode)

    Returns:
        list of conflicting fields

    Example Usage:
        >>> account_api.check_account_exists(username="bob")
        []
        >>> account_api.check_account_exists(username="ted", email="ted@example.com")
        ["email", "username"]

    """
    conflicts = []

    try:
        _validate_email_doesnt_exist(email)
    except AccountEmailAlreadyExists:
        conflicts.append("email")
    try:
        _validate_username_doesnt_exist(username)
    except AccountUsernameAlreadyExists:
        conflicts.append("username")

    return conflicts


@intercept_errors(UserAPIInternalError, ignore_errors=[UserAPIRequestError])
def activate_account(activation_key):
    """Activate a user's account.

    Args:
        activation_key (unicode): The activation key the user received via email.

    Returns:
        None

    Raises:
        UserNotAuthorized
        UserAPIInternalError: the operation failed due to an unexpected error.

    """
    try:
        registration = Registration.objects.get(activation_key=activation_key)
    except Registration.DoesNotExist:
        raise UserNotAuthorized
    else:
        # This implicitly saves the registration
        registration.activate()


@intercept_errors(UserAPIInternalError, ignore_errors=[UserAPIRequestError])
def request_password_change(email, orig_host, is_secure):
    """Email a single-use link for performing a password reset.

    Users must confirm the password change before we update their information.

    Args:
        email (str): An email address
        orig_host (str): An originating host, extracted from a request with get_host
        is_secure (bool): Whether the request was made with HTTPS

    Returns:
        None

    Raises:
        UserNotFound
        AccountRequestError
        UserAPIInternalError: the operation failed due to an unexpected error.

    """
    # Binding data to a form requires that the data be passed as a dictionary
    # to the Form class constructor.
    form = PasswordResetFormNoActive({'email': email})

    # Validate that a user exists with the given email address.
    if form.is_valid():
        # Generate a single-use link for performing a password reset
        # and email it to the user.
        form.save(
            from_email=configuration_helpers.get_value('email_from_address', settings.DEFAULT_FROM_EMAIL),
            domain_override=orig_host,
            use_https=is_secure
        )
    else:
        # No user with the provided email address exists.
        raise UserNotFound


def get_username_validation_error(username, default=''):
    """Get the built-in validation error message for when
    the username is invalid in some way.

    :param username: The proposed username (unicode).
    :param default: The message to default to in case of no error.
    :return: Validation error message.

    """
    try:
        _validate_username(username)
    except AccountUsernameInvalid as invalid_username_err:
        return invalid_username_err.message
    return default


def get_email_validation_error(email, default=''):
    """Get the built-in validation error message for when
    the email is invalid in some way.

    :param email: The proposed email (unicode).
    :param default: The message to default to in case of no error.
    :return: Validation error message.

    """
    try:
        _validate_email(email)
    except AccountEmailInvalid as invalid_email_err:
        return invalid_email_err.message
    return default


def get_password_validation_error(password, username=None, default=''):
    """Get the built-in validation error message for when
    the password is invalid in some way.

    :param password: The proposed password (unicode).
    :param username: The username associated with the user's account (unicode).
    :param default: The message to default to in case of no error.
    :return: Validation error message.

    """
    try:
        _validate_password(password, username)
    except AccountPasswordInvalid as invalid_password_err:
        return invalid_password_err.message
    return default


def get_username_existence_validation_error(username, default=''):
    """Get the built-in validation error message for when
    the username has an existence conflict.

    :param username: The proposed username (unicode).
    :param default: The message to default to in case of no error.
    :return: Validation error message.

    """
    try:
        _validate_username_doesnt_exist(username)
    except AccountUsernameAlreadyExists as username_exists_err:
        return username_exists_err.message
    return default


def get_email_existence_validation_error(email, default=''):
    """Get the built-in validation error message for when
    the email has an existence conflict.

    :param email: The proposed email (unicode).
    :param default: The message to default to in case of no error.
    :return: Validation error message.

    """
    try:
        _validate_email_doesnt_exist(email)
    except AccountEmailAlreadyExists as email_exists_err:
        return email_exists_err.message
    return default


def _get_user_and_profile(username):
    """
    Helper method to return the legacy user and profile objects based on username.
    """
    try:
        existing_user = User.objects.get(username=username)
    except ObjectDoesNotExist:
        raise UserNotFound()

    existing_user_profile, _ = UserProfile.objects.get_or_create(user=existing_user)

    return existing_user, existing_user_profile


def _validate_username(username):
    """Validate the username.

    Arguments:
        username (unicode): The proposed username.

    Returns:
        None

    Raises:
        AccountUsernameInvalid

    """
    try:
        _validate_unicode(username)
        _validate_type(username, basestring, USERNAME_BAD_TYPE_MSG)
        _validate_length(username, USERNAME_MIN_LENGTH, USERNAME_MAX_LENGTH, USERNAME_BAD_LENGTH_MSG)
        with override_language('en'):
            # `validate_username` provides a proper localized message, however the API needs only the English
            # message by convention.
            student_forms.validate_username(username)
    except (UnicodeError, AccountDataBadType, AccountDataBadLength, ValidationError) as invalid_username_err:
        raise AccountUsernameInvalid(invalid_username_err.message)


def _validate_email(email):
    """Validate the format of the email address.

    Arguments:
        email (unicode): The proposed email.

    Returns:
        None

    Raises:
        AccountEmailInvalid

    """
    try:
        _validate_unicode(email)
        _validate_type(email, basestring, EMAIL_BAD_TYPE_MSG)
        _validate_length(email, EMAIL_MIN_LENGTH, EMAIL_MAX_LENGTH, EMAIL_BAD_LENGTH_MSG)
        validate_email.message = EMAIL_INVALID_MSG.format(email=email)
        validate_email(email)
    except (UnicodeError, AccountDataBadType, AccountDataBadLength, ValidationError) as invalid_email_err:
        raise AccountEmailInvalid(invalid_email_err.message)


def _validate_password(password, username=None):
    """Validate the format of the user's password.

    Passwords cannot be the same as the username of the account,
    so we take `username` as an argument.

    Arguments:
        password (unicode): The proposed password.
        username (unicode): The username associated with the user's account.

    Returns:
        None

    Raises:
        AccountPasswordInvalid

    """
    try:
        _validate_type(password, basestring, PASSWORD_BAD_TYPE_MSG)
        _validate_length(password, PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH, PASSWORD_BAD_LENGTH_MSG)
        _validate_password_works_with_username(password, username)
    except (AccountDataBadType, AccountDataBadLength) as invalid_password_err:
        raise AccountPasswordInvalid(invalid_password_err.message)


def _validate_username_doesnt_exist(username):
    """Validate that the username is not associated with an existing user.

    :param username: The proposed username (unicode).
    :return: None
    :raises: AccountUsernameAlreadyExists
    """
    if username is not None and User.objects.filter(username=username).exists():
        raise AccountUsernameAlreadyExists(_(USERNAME_CONFLICT_MSG).format(username=username))


def _validate_email_doesnt_exist(email):
    """Validate that the email is not associated with an existing user.

    :param email: The proposed email (unicode).
    :return: None
    :raises: AccountEmailAlreadyExists
    """
    if email is not None and User.objects.filter(email=email).exists():
        raise AccountEmailAlreadyExists(_(EMAIL_CONFLICT_MSG).format(email_address=email))


def _validate_password_works_with_username(password, username=None):
    """Run validation checks on whether the password and username
    go well together.

    An example check is to see whether they are the same.

    :param password: The proposed password (unicode).
    :param username: The username associated with the user's account (unicode).
    :return: None
    :raises: AccountPasswordInvalid
    """
    if password == username:
        raise AccountPasswordInvalid(PASSWORD_CANT_EQUAL_USERNAME_MSG)


def _validate_type(data, type, err):
    """Checks whether the input data is of type. If not,
    throws a generic error message.

    :param data: The data to check.
    :param type: The type to check against.
    :param err: The error message to throw back if data is not of type.
    :return: None
    :raises: AccountDataBadType

    """
    if not isinstance(data, type):
        raise AccountDataBadType(err)


def _validate_length(data, min, max, err):
    """Validate that the data's length is less than or equal to max,
    and greater than or equal to min.

    :param data: The data to do the test on.
    :param min: The minimum allowed length.
    :param max: The maximum allowed length.
    :return: None
    :raises: AccountDataBadLength

    """
    if len(data) < min or len(data) > max:
        raise AccountDataBadLength(err)


def _validate_unicode(data, err=u"Input not valid unicode"):
    """Checks whether the input data is valid unicode or not.

    :param data: The data to check for unicode validity.
    :param err: The error message to throw back if unicode is invalid.
    :return: None
    :raises: UnicodeError

    """
    try:
        if not isinstance(data, str) and not isinstance(data, unicode):
            raise UnicodeError
        # In some cases we pass the above, but it's still inappropriate utf-8.
        unicode(data)
    except UnicodeError:
        raise UnicodeError(err)
