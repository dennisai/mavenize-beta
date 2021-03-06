from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.base import ContentFile
from django.db.models import get_model, F
from django.template.defaultfilters import slugify

from activity_feed.models import Activity
from bookmark.models import Bookmark
from notification.models import Notification
from leaderboard.models import KarmaAction
from social_auth.models import UserSocialAuth
from social_graph.models import Forward, Backward
from user_profile.models import UserProfile, UserStatistics

from announce import AnnounceClient
import cacheAPI
from celery.task import task
import facebook
import hashlib
from urllib2 import urlopen, HTTPError

MODEL_APP_NAME = {
    'user': 'auth',
    'userstatistics': 'user_profile',
    'backward': 'social_graph',
    'review': 'review',
    'agree': 'review',
    'thank': 'review',
    'item': 'item',
    'popularity': 'item',
    'movie': 'movie',
    'activity': 'activity_feed',
    'notification': 'notification'
}

announce_client = AnnounceClient()

"""
GET METHODS
"""
def filter_then_order_by(model_name, order_criteria, **filters):
    """
    Filters a model with filters and orders the results by
    order_criteria.
        model_name: string of class name
        order_criteria: string of field name
        **filters: dictionary that maps fields to criterion
            Ex.: { 'user__pk': 1 }
    """
    model = get_model(MODEL_APP_NAME[model_name], model_name)
    return model.objects.filter(**filters).order_by(order_criteria)


def filter_excluding_me_then_order_by(model_name, obj_id,
                                      order_criteria, **filters):
    """
    Filters a model with filters, excluding obj_id, and orders the
    results by order_criteria.
        model_name: string of class name
        obj_id: the pk to be excluded
        order_criteria: string of field name
        **filters: dictionary that maps fields to criterion
            Ex.: { 'user__pk': 1 }
    """
    model = get_model(MODEL_APP_NAME[model_name], model_name)
    return model.objects.filter(**filters) \
                        .exclude(pk=obj_id) \
                        .order_by(order_criteria)


def filter_then_count(model_name, **filters):
    """
    Filters a model with filters and returns the object count.
    """
    model = get_model(MODEL_APP_NAME[model_name], model_name)
    return model.objects.filter(**filters).count()


def get_content_type(model_name):
    """
    Returns the ContentType for a model name.
    """
    return ContentType.objects.get(
        app_label=MODEL_APP_NAME[model_name], model=model_name)


def get_object(model_name, **filters):
    """
    Filters a model with filters and returns a single object.
    """
    model = get_model(MODEL_APP_NAME[model_name], model_name)
    return model.objects.get(**filters)


"""
CREATE METHODS
"""
def queue_activity(sender_id, verb, model_name, obj_id):
    """
    Adds a review or agree to the activity feed.
        sender_id: review.user_id or agree.user_id
        verb: either "raved about" or "re-raved"
        model_name: string of class name 
        obj_id: object.pk
    """
    model = get_model(MODEL_APP_NAME[model_name], model_name)
    try:
        Activity.objects.create(
            sender=User.objects.get(pk=sender_id),
            verb=verb,
            target_object=model.objects.get(pk=obj_id)
        )
    except ObjectDoesNotExist:
        pass


def queue_notification(sender_id, recipient_id, model_name, obj_id):
    """
    Adds a notification for an agree, thanks, bookmark, or follow.
    Increments the notification count in the cache.
        sender_id: agree.giver_id, thank.giver_id, bookmark.user_id,
            or backward.source_id
        recipient_id: agree.review.user_id, thank.review.user_id,
            0, or backward.destination_id
        model_name: string of class name
        obj_id: object.pk
    """
    model = get_model(MODEL_APP_NAME[model_name], model_name)
    try:
        pg_notification = Notification(
            sender=User.objects.get(pk=sender_id),
            recipient=User.objects.get(pk=recipient_id),
            notification_type=model_name
        )
        if model_name == "agree" or model_name == "thank":
            obj = model.objects.get(pk=obj_id)
            pg_notification.item = obj.review.item
            if model_name == "thank":
                pg_notification.note = obj.note
        pg_notification.save()

        if not cacheAPI._get_new_notifications_count(recipient_id):
            cacheAPI._reset_new_notifications_count(recipient_id)
        cacheAPI._increment_new_notifications_count(recipient_id)
        cacheAPI._cache_notification_for_user(pg_notification)
        announce_client.emit(
            recipient_id,
            'notification',
            data={ 'new': 1 } # Currently un-used
        )

    except ObjectDoesNotExist:
        pass

def add_karma_action(recipient_id, giver_id, karma):
    """
    Adds a karma action for a review, thanks, or agree.
        recipient_id: review.user_id, agree.review.user_id, or
            thank.review.user_id
        giver_id: review.user_id, agree.giver_id, or thank.giver_id
        karma: integer value > 0
    """
    try: 
        KarmaAction.objects.create(
            recipient=User.objects.get(pk=recipient_id),
            giver=User.objects.get(pk=giver_id),
            karma=karma
        )
    except ObjectDoesNotExist:
        pass


def queue_bookmark_notifications(user_id, item_id):
    """
    Emits a notification via SocketIO to the friends of the user
    who have bookmarked the same item.
        user_id: the primary key of the user (integer)
        item_id: the primary key of the item (integer)
    """
    following = list(Forward.objects.filter(source_id=user_id)
                            .values_list('destination_id', flat=True))
    followers =list(Backward.objects.filter(destination_id=user_id)
                            .values_list('source_id', flat=True))
    friends = list(set(following) & set(followers))
    to_notify = Bookmark.objects.filter(user__in=friends, item=item_id) \
                                .values_list('user_id', flat=True)
    for user in to_notify:
        if not cacheAPI._get_new_bookmarks_count(user):
            cacheAPI._reset_new_bookmarks_count(user)
        cacheAPI._increment_new_bookmarks_count(user)
        announce_client.register_group(user, 'bookmarks')
    announce_client.broadcast_group(
        group_name='bookmarks',
        channel='bookmark',
        data = { 'new': 1 } # Currently un-used
    )
    for user in to_notify:
        announce_client.unregister_group(user, 'bookmarks')


def create_user_profile(user_id, facebook_id):
    """
    Creates the user profile and statistics for the user as well as  
    the avatar and thumbnail.
        user_id: the primary key of the user (integer)
        facebook_id: the facebook id of the user (integer)
    """
    user = User.objects.get(pk=user_id)
    profile, created = UserProfile.objects.get_or_create(user=user)
    statistics, created = UserStatistics.objects.get_or_create(
        user=user)
    update_facebook_profile_picture.delay(user_id, facebook_id, created)


@task(ignore_result=True)
def update_facebook_profile_picture(user_id, facebook_id, is_created):
    """
    Saves the avatar and thumbnail of a Facebook user.
        user_id: the primary key of the user (integer)
        facebook_id: the facebook id of the user (integer)
        is_created: True if the profile was created (boolean)
    """
    try:
        profile = UserProfile.objects.get(pk=user_id)
        url = ("http://graph.facebook.com/%s/picture" %         
            facebook_id)
        avatar = urlopen(url+'?type=large', timeout=30).read()
        thumbnail = urlopen(url, timeout=30).read()
        if not is_created and profile.thumbnail:
            if (hashlib.sha1(profile.thumbnail.read()).digest()
                    != hashlib.sha1(thumbnail).digest()):
                profile.avatar.delete()
                profile.thumbnail.delete()
                profile.avatar.save(
                    slugify(str(user_id)+'a')+'.jpg',
                    ContentFile(avatar)
                )
                profile.thumbnail.save(
                    slugify(str(user_id)+'t') + '.jpg',     
                    ContentFile(thumbnail)
                )
        else:
            profile.avatar.save(
                slugify(str(user_id)+'a')+'.jpg',
                ContentFile(avatar)
            )
            profile.thumbnail.save(
                slugify(str(user_id)+'t') + '.jpg',     
                ContentFile(thumbnail)
            )
    except HTTPError:
        pass
    

@task(ignore_result=True)
def build_social_graph(user_id):
    """
    Uses the user's social graph on Facebook to generate following
    and follower relationships that don't already exist.
        user_id: the primary key of the user (integer)
    """
    access_token = User.objects.get(pk=user_id) \
                               .social_auth.get(provider='facebook') \
                               .extra_data['access_token']
    graph = facebook.GraphAPI(access_token)
    friends = graph.get_connections("me", "friends")['data']
    friend_ids = [friend['id'] for friend in friends]
    signed_up = UserSocialAuth.objects.filter(uid__in=friend_ids) \
                                      .values_list('user_id', flat=True)
    already_following = Forward.objects.filter(source_id=user_id) \
                                       .values_list('destination_id',
                                                    flat=True)
    to_add = list(set(signed_up) - set(already_following))
    # Need to create notifications for these because bulk_create does not
    # trigger signals.
    
    forward_connections = [Forward(source_id=user_id, destination_id=fid)
        for fid in to_add]
    forward_connections += [Forward(source_id=fid, destination_id=user_id)
        for fid in to_add]
    backward_connections = [Backward(destination_id=fid, source_id=user_id)
        for fid in to_add]
    backward_connections = [Backward(destination_id=user_id, source_id=fid)
        for fid in to_add]
    Forward.objects.bulk_create(forward_connections)
    Backward.objects.bulk_create(backward_connections)

"""
UPDATE METHODS
"""
def update_statistics(model_name, obj_id, **fields):
    """
    Updates the statistics for a user, review or item after a
    review, thanks, or agree.
        model_name: string of class name
        obj_id: object.pk
        **fields: dictionary that maps fields to integer values
            Ex.: fields = {'karma': 1}
    """
    model = get_model(MODEL_APP_NAME[model_name], model_name)
    model.objects.filter(pk=obj_id).update(
        **dict([(k, F(k)+fields[k]) for k in fields.keys()]))


"""
DELETE METHODS
"""
def remove_activity(sender_id, verb, model_name, obj_id):
    """
    Removes a review (and all related agrees) or agree from the
    activity feed.
        sender_id: review.user_id or agree.user_id
        verb: either "raved about" or "re-raved"
        model_name: string of class name
        obj_id: object.pk
    """
    Activity.objects.get(
        sender=User.objects.get(pk=sender_id),
        verb=verb,
        content_type=ContentType.objects.get(
            app_label=MODEL_APP_NAME[model_name],
            model=model_name),
        object_id=obj_id
    ).delete()

def remove_karma_action(recipient_id, giver_id, karma, time_range):
    """
    Removes a karma action for a review, thanks, or agree.
        recipient_id: review.user_id, agree.review.user_id, or
            thank.review.user_id
        giver_id: review.user_id, agree.giver_id, or thank.giver_id
        karma: integer value > 0
        time_range: tuple of datetime objects - (start, end)
    """
    try:
        KarmaAction.objects.filter(
            recipient=User.objects.get(pk=recipient_id),
            giver=User.objects.get(pk=giver_id),
            karma=karma,
            created_at__range=time_range
        )[0].delete()
    except ObjectDoesNotExist:
        pass

def filter_then_delete(model_name, **filters):
    """
    Filters a model with filters and deletes those objects. 
        model_name: string of class name
        **filters: dictionary that maps fields to criterion
            Ex.: { 'user__pk': 1 }
    """
    model = get_model(MODEL_APP_NAME[model_name], model_name)
    model.objects.filter(**filters).delete()
