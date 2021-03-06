from django.contrib import messages
from django.contrib.auth.models import User
from django.core.paginator import Paginator, EmptyPage, InvalidPage
from django.core.urlresolvers import reverse
from django.db.models import Sum, Count
from django.template.defaultfilters import slugify
from django.utils import simplejson
from django.utils.html import escape, linebreaks
from django.utils.timesince import timesince

from activity_feed.models import Activity
from bookmark.models import Bookmark, BookmarkGroup
from item.models import Item
from leaderboard.models import KarmaAction
from movie.models import Movie, Genre, Actor, Director
from notification.models import Notification
from review.models import Review, Agree, Thank, ReviewForm, ThankForm
from social_graph.models import Forward, Backward
from user_profile.models import UserProfile, UserStatistics
from sorl.thumbnail import get_thumbnail
from utils import QuerySetChain
import datetime as dt
import cacheAPI

MESSAGES = {
    'agree': 'just re-raved you on',
    'thank': 'thanked you for your rave on',
    'backward': 'is now following you.'
}

"""
GET METHODS
"""
def get_profile(user_id):
    """
    Returns the user, user profile, and user statistics for the
    specified user.
    """
    return User.objects.select_related('userprofile', 'userstatistics') \
                       .get(pk=user_id)

def get_following(user_id):
    """
    Returns a list of user ids who the specified user is following.
        user_id: primary key of the user (integer)
    """
    return list(Forward.objects.filter(source_id=user_id).values_list(
        'destination_id', flat=True))


def get_followers(user_id):
    """
    Returns a list of user ids who follow the specified user.
        user_id: primary key of the user (integer)
    """
    return list(Backward.objects.filter(destination_id=user_id) \
                                .values_list('source_id', flat=True))

def get_mavens(user_id):
    """
    Returns a list of the top ranked users by karma for a given user.
        user_id: primary key of the user (integer)
    """
    following = get_following(user_id)
    return list(User.objects.exclude(pk__in=(following + [user_id])) \
                            .order_by('-userstatistics__karma') \
                            .values_list('id', flat=True))


def get_friends(user_id):
    """
    Returns a list of users who the follow the user AND ALSO the user
    follows them.
        user_id: primary key of the user (integer)
    """
    return list(set(get_following(user_id)) &
                set(get_followers(user_id)))

def get_latest_movie():
    """
    Returns the most recent movie with a picture in the database.
    """
    return Movie.objects.exclude(image__contains="default.jpg") \
                        .latest('theater_date')


def get_bookmarked_items(user_id):
    """
    Returns a list of item ids of bookmarks for the specified user.
        user_id: primary key of the user (integer)
    """
    return list(Bookmark.objects.filter(user=user_id).values_list(
        'item_id', flat=True))

def get_user_activity(my_id, user_ids, page):
    """
    Returns the activities of the users specified in a list of user
    IDs in JSON.
        user_ids: primary keys of users (list of integers)
        page: page number (integer)
    """
    activities = Activity.objects.select_related('sender',
                                                 'sender__userprofile') \
                    .prefetch_related('target_object',
                                      'target_object__user',
                                      'target_object__item',
                                      'target_object__item__movie') \
                    .filter(sender__in=user_ids)
    bookmarks = get_bookmarked_items(my_id)

    paginator = Paginator(activities, 20)
    
    try:
        next_page = paginator.page(page).next_page_number()
        paginator.page(next_page)
    except (EmptyPage, InvalidPage):
        next_page = ''

    response = [{
        'object_id': activity.object_id,
        'sender_avatar': get_thumbnail(
            activity.sender.userprofile.avatar, '100x100', crop='center').url,
        'rating': activity.target_object.rating,
        'target_url': reverse('movie-profile',
            args=[activity.target_object.item.movie.url]),
        'target_image': get_thumbnail(
            activity.target_object.item.movie.image, 'x285').url,
        'target_title': activity.target_object.item.movie.title,
        'sender_id': activity.sender_id,
        'sender_url': reverse('user-profile', args=[activity.sender_id]),
        'sender_full_name': activity.sender.get_full_name(),
        'verb': activity.verb,
        'target_user_id': activity.target_object.user_id,
        'target_user_url': reverse('user-profile',
            args=[activity.target_object.user_id]),
        'target_user_full_name': activity.target_object.user \
                                         .get_full_name(),
        'target_user_first_name': activity.target_object.user \
                                          .first_name.lower(),
        'text': linebreaks(escape(activity.target_object.text)),
        'time_since': timesince(activity.created_at),
        'item_id': activity.target_object.item_id,
        'bookmarked': (True if activity.target_object.item_id in 
                            bookmarks else False),
        'next': next_page 
    } for activity in paginator.page(page)]

    return simplejson.dumps(response)
 

def get_beneficiary_leaderboard(user_id, start_time):
    """
    Returns the beneficiary leaderboard for a given user between the
    starting time and now.
    """
    beneficiary_rankings = \
        KarmaAction.objects.filter(created_at__gte=start_time) \
                           .filter(recipient=user_id) \
                           .exclude(giver=user_id) \
                           .values('giver') \
                           .annotate(total_given=Sum('karma')) \
                           .order_by('-total_given')[:5]
    
    return _match_users_with_karma(beneficiary_rankings,
        'giver', 'total_given')


def get_relative_leaderboard(user_id, start_time):
    """
    Returns the relative leaderboard rankings and start index for a
    given user between starting time and now.
        user_id: primary key of the user (integer)
        start_time: starting time (datetime.datetime)
    """
    us = get_following(user_id) + [user_id]
    leaderboard_rankings = \
        KarmaAction.objects.filter(created_at__gte=start_time) \
                           .filter(giver__in=us) \
                           .values('recipient') \
                           .annotate(total_received=Sum('karma')) \
                           .order_by('-total_received')

    try:
        my_ranking = [i for i, v in enumerate(leaderboard_rankings)
            if v['recipient'] == user_id][0]
    except IndexError:
        my_ranking = 0

    start, end = _compute_relative_leaderboard_indexes(my_ranking,
        len(leaderboard_rankings))
    
    return (_match_users_with_karma(
                leaderboard_rankings[start:end],
                'recipient',
                'total_received'),
            start)


def _match_users_with_karma(rankings, user_key, karma_key):
    """
    Returns a list of tuples that maps User objects to karma.
        rankings: list of dictionaries that contains user_ids
            (integer) and karma (integer)
    """
    if not rankings:
        return []

    giver_ids = [r[user_key] for r in rankings]
    ids_to_users = User.objects.select_related(
        'userprofile').in_bulk(giver_ids)
    return [(ids_to_users[r[user_key]], r[karma_key]) \
        for r in rankings]


def _compute_relative_leaderboard_indexes(ranking, size):
    """
    Returns a tuple of the start and end indexes for the relative
    leaderboard.
        ranking: ranking of the user (integer)
        size: the number of users in the leaderboard (integer)
    """
    if ranking == 0 or ranking == 1:
        return (0, 5)
    elif ranking == size or ranking == size-1:
        return (max(0, size-5), size)
    else:
        return (max(0, ranking-2), max(size, ranking+3))


def get_movie_thumbnails(time_period, page, filters):
    """
    Returns a list of movie thumbnails and other relevant information
    based on a time period, page, and a set of filters.
        time_period: 'today', 'week', 'month', or 'alltime' (string)
        page: page for the paginator (integer)
        filters: dictionary that maps fields to parameters (dict)
    """
    movies = Movie.objects.filter(**filters) \
            .order_by('-item__popularity__' + time_period) \
            .values('title', 'url', 'synopsis', 'image', 'theater_date') \
            .distinct()
    paginator = Paginator(movies, 12)

    try:
        next_page = paginator.page(page).next_page_number()
        paginator.page(next_page)
    except (EmptyPage, InvalidPage):
        next_page = ''

    response = [{ 
        'title': escape(movie['title']),
        'url': reverse('movie-profile', args=[movie['url']]),
        'synopsis': escape(movie['synopsis'][:140]),
        'image_url': get_thumbnail(movie['image'], 'x285').url,
        'next': next_page 
    } for movie in paginator.page(page)] 

    return simplejson.dumps(response)


def get_user_boxes(my_id, user_ids, page):
    """
    Returns a list of user details required for following and follower
    boxes.
        my_id: user id of the current user (integer)
        user_ids: list of user ids (integers)
        page: page for the paginator (integer)
    """
    my_following = get_following(my_id)
    profiles = UserProfile.objects.select_related('user') \
                                  .filter(pk__in=user_ids)
    paginator = Paginator(profiles, 12)
    current_page_user_ids = [profile.pk for profile in
        paginator.page(page)]
    are_following = list(set(my_following) & set(current_page_user_ids))

    previous_page, next_page = "", ""
    current_page = paginator.page(page)
    if current_page.has_previous():
        previous_page = current_page.previous_page_number()
    if current_page.has_next():
        next_page = current_page.next_page_number()
    
    response = [{
        'id': profile.pk,
        'full_name': profile.user.get_full_name(),
        'about_me': escape(profile.about_me),
        'image_url': get_thumbnail(profile.avatar, '100x100',
            crop='center').url,
        'url': reverse('user-profile', args=[profile.pk]),
        'is_following': True if profile.pk in are_following else False,
        'previous': previous_page,
        'next': next_page 
    } for profile in paginator.page(page)]

    return simplejson.dumps(response)


def is_following(source_id, destination_id):
    """
    Returns True if the source id is following the destination id.
        source_id: user id of the source (integer)
        destination_id: user id of the destination (integer)
    """
    if Forward.objects.filter(source_id=source_id,
                              destination_id=destination_id):
        return True
    return False


def get_new_notifications_count(user_id):
    """
    Returns the number of new notifications for a user.
        user_id: primary key of the user (integer)
    """
    return simplejson.dumps(
        cacheAPI._get_new_notifications_count(user_id))

def get_recent_notifications(user_id):
    """
    Returns the last five notifications for a user.
        user_id: primary key of the user (integer)
    """
    raw_notifications = cacheAPI._get_notifications(user_id)
    response = [{
        'user_name': notification['sender_name'],
        'user_url': reverse('user-profile',
            args=[notification['sender_id']]),
        'user_avatar': get_thumbnail(
                            'img/users/thumbnails/' + \
                                str(notification['sender_id']) + 't.jpg',
                            '25x25',
                            crop='center'
                        ).url,
        'message': MESSAGES[notification['notification_type']],
        'item_name': notification.get('item_name', ''),
        'item_url': (reverse(notification['item_type']+'-profile',
                args=[slugify(notification.get('item_name', 'none'))])
                        if notification.get('item_type') else ''),
        'time_since': timesince(notification['timestamp'])
    } for notification in raw_notifications]

    return simplejson.dumps(response)


def get_notifications(user_id, page):
    """
    Returns a list of notifications for a user.
        user_id: primary key of the user (integer)
        page: page for the paginator (integer)
    """
    notifications = \
        Notification.objects.select_related('sender',
                                            'sender__userprofile',
                                            'item',
                                            'item__movie') \
                            .filter(recipient=user_id) \
                            .order_by('-created_at')
    paginator = Paginator(notifications, 20)

    try:
        next_page = paginator.page(page).next_page_number()
        paginator.page(next_page)
    except (EmptyPage, InvalidPage):
        next_page = ''

    response = [_generate_notification_response(notification, next_page) 
        for notification in paginator.page(page)]

    return simplejson.dumps(response)

def _generate_notification_response(notification, next_page):
    """
    Converts a notification into a Python dictionary that will
    be returned as JSON.
        notification: Notification object
    """
    response = {
        'user_name': notification.sender.get_full_name(),
        'user_url': reverse('user-profile',
            args=[notification.sender_id]),
        'user_avatar': notification.sender.userprofile.thumbnail.url,
        'message': MESSAGES[notification.notification_type],
        'time_since': timesince(notification.created_at),
        'next': next_page 
    }
    if (notification.notification_type == "thank" or 
            notification.notification_type == "agree"):
        item_type = notification.item.item_type
        response['item_name'] = getattr(notification.item, item_type).__str__()
        response['item_url'] = reverse(item_type+'-profile',
            args=[slugify(response['item_name'])])
        if notification.notification_type == "thank":
            response['thank_you'] = escape(notification.note)
    
    return response


def get_new_bookmarks_count(user_id):
    """
    Returns the number of new bookmarks for a user.
        user_id: primary key of the user (integer)
    """
    return simplejson.dumps(
        cacheAPI._get_new_bookmarks_count(user_id))


def get_bookmarked_movies(user_id, page):
    """
    Returns the bookmarks of the user for the bookmarks modal.
        user_id: primary key of the user (integer)
        page: page number (integer)
    """
    friends = get_friends(user_id)
    my_bookmarks = get_bookmarked_items(user_id)
    last_checked = (cacheAPI._get_bookmarks_last_checked(user_id) or
                    dt.datetime.min)

    recent_bookmarks = Movie.objects.filter(
            item__pk__in=my_bookmarks,
            item__bookmark__created_at__gte=last_checked,
            item__bookmark__user__in=friends)
    print recent_bookmarks
    print last_checked
    recent_item_ids = recent_bookmarks.values_list('item', flat=True)
    not_recent = list(set(my_bookmarks) - set(recent_item_ids))
    other_bookmarks = Movie.objects.filter(item__pk__in=not_recent) \
                                   .values('item_id', 'url', 'image')
    annotated = recent_bookmarks.values('item_id', 'url', 'image') \
            .annotate(new_bookmarks=Count('item__bookmark__user')) \
            .order_by('-new_bookmarks')
    combined = QuerySetChain(annotated, other_bookmarks)
    paginator = Paginator(combined, 12)

    previous_page, next_page = "", ""
    current_page = paginator.page(page)
    if current_page.has_previous():
        previous_page = current_page.previous_page_number()
    if current_page.has_next():
        next_page = current_page.next_page_number()

    response = [{
        'item_id': movie['item_id'],
        'url': movie['url'],
        'image_url': get_thumbnail(movie['image'], 'x285').url,
        'new_bookmarks': movie.get('new_bookmarks', 0),
        'previous': previous_page,
        'next': next_page
    } for movie in paginator.page(page)]

    return simplejson.dumps(response)


def get_friend_bookmarks(user_id, item_id):
    """
    Returns the friends who have bookmarked the movie title.
        user_id: primary key of the user (integer)
        item_id: primary key of the item (integer) 
    """
    friends = get_friends(user_id)
    bookmarks = Bookmark.objects.select_related('user', 'userprofile') \
                                .filter(user__in=friends, item=item_id) \
                                .order_by('-created_at')

    response = [{
        'user_name': bookmark.user.get_full_name(),
        'user_url': reverse('user-profile', args=[bookmark.user_id]),
        'user_thumbnail': bookmark.user.userprofile.thumbnail.url
    } for bookmark in bookmarks]

    return simplejson.dumps(response)

"""
CREATE METHODS
"""
def follow(source_id, destination_id):
    """
    Creates a following relationship between the source id and the
    destination id.
        source_id: user id of the source (integer)
        destination_id: user id of the destination (integer)
    """
    if source_id == destination_id:
        return "You can't follow yourself!"

    Forward.objects.get_or_create(source_id=source_id,
                                  destination_id=destination_id)
    Backward.objects.get_or_create(destination_id=destination_id,
                                   source_id=source_id)

def about_me(user_id, text):
    """
    Fills in the about me section of the user profile for a given user.
        user_id: primary key of the user (integer)
        text: text of the about me section (string)
    """
    UserProfile.objects.filter(pk=user_id).update(about_me=text)
    

def bookmark(user_id, item_id):
    """
    Creates a bookmark for a user on a given item if he hasn't already
    bookmarked it.
        user_id: primary key of the user (integer)
        item_id: primary key of the item (integer)
    """
    Bookmark.objects.get_or_create(user=User.objects.get(pk=user_id),
                                   item=Item.objects.get(pk=item_id))


def review(user_id, item_id, text, rating):
    """
    Creates a review for an item if the user has previously not
    written a review for the item.  Returns False if the review
    has been created successfully, otherwise returns an error
    message.
        user_id: id of the reviewer (integer)
        item_id: id of the item (integer)
        text: text of the review (string)
        rating: rating of the review (integer)
    """
    if Review.objects.filter(user=user_id, item=item_id):
        return "You already wrote a review!"

    form = ReviewForm({
        'user': user_id,
        'item': item_id,
        'text': text,
        'rating': rating,
        'agrees': 0,
        'thanks': 0
    })
    if form.is_valid():
        form.save()
        return False
    return "Something was wrong with the review you submitted!"


def agree(user_id, review_id):
    """
    Creates an agree for a review if the user has previously not
    agreed with the review and the user is not the writer of the
    review.  Returns False if the agree has been created successfully,
    otherwise returns an error message.
        user_id: id of the agreer (integer)
        review_id: id of the review (integer)
    """
    review = Review.objects.get(pk=review_id)
    if user_id == review.user_id:
        return "You can't re-rave your own review!"

    agree, created = Agree.objects.get_or_create(
        giver=User.objects.get(pk=user_id),
        review=review
    )
    if created:
        return False
    return "You already re-raved this review!"


def thank(user_id, review_id, note):
    """
    Creates a thank for a review if the user has previously not
    thanked the review and the user is not the writer of the
    review.  Returns False if the agree has been created successfully,
    otherwise returns an error message.
        user_id: id of the thanker (integer)
        review_id: id of the review (integer)
        note: thank you note (string)
    """
    if (Thank.objects.filter(giver=user_id, review=review_id) or
            user_id == Review.objects.get(pk=review_id).user_id):
        return "You can't thank your own review!"
    
    form = ThankForm({
        'giver': user_id,
        'review': review_id,
        'note': note
    })
    if form.is_valid():
        form.save()
        return False
    return "You already thanked this review!"

"""
UPDATE METHODS
"""
def reset_new_notifications_count(user_id):
    """
    Resets the number of new notifications for a user to zero.
        user_id: primary key of the user (integer)
    """
    cacheAPI._reset_new_notifications_count(user_id)

def reset_new_bookmarks_count(user_id):
    """
    Resets the number of new bookmarks for a user to zero.
        user_id: primary key of the user (integer)
    """
    cacheAPI._reset_new_bookmarks_count(user_id)

"""
DELETE METHODS
"""
def unfollow(source_id, destination_id):
    """
    Deletes a following relationship between the source id and the
    destination id.
        source_id: user id of the source (integer)
        destination_id: user id of the destination (integer)
    """
    Forward.objects.filter(source_id=source_id,
                           destination_id=destination_id).delete()
    Backward.objects.filter(destination_id=destination_id,
                            source_id=source_id).delete()

def unbookmark(user_id, item_id):
    """
    Deletes a bookmark for a user on a given item.
        user_id: primary key of the user (integer)
        item_id: primary key of the item (integer)
    """
    Bookmark.objects.filter(user=user_id, item=item_id).delete()
