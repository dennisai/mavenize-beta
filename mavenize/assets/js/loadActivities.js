(function($) {
  // Templates
  var activityTemplate = _.template("\
    <% for (var i = 0; i < activities.length; i++) { %>\
      <% var activity = activities[i]; %>\
      <li class='activity' value='<%= activity.object_id %>' data-next='<%= activity.next %>'>\
        <div class='user-avatar pull-left'>\
          <a href='<%= activity.sender_url %>'>\
            <img src='<%= activity.sender_avatar %>' width='100' height='100' />\
          </a>\
          <img src='<%= STATIC_URL %>img/<%= activity.rating %>l.png' />\
        </div>\
        <div class='item-thumbnail pull-right span2'>\
          <a class='thumbnail' href='<%= activity.target_url %>'>\
            <img src='<%= activity.target_image %>' />\
          </a>\
          <% if (activity.bookmarked) { %>\
            <button class='btn btn-warning btn-bookmark' value='<%= activity.item_id %>'>Remove It</button>\
          <% } else { %>\
            <button class='btn btn-success btn-bookmark' value='<%= activity.item_id %>'>Add to Scrapbook</button>\
          <% } %>\
        </div>\
        <div class='review-context'>\
          <a href='<%= activity.sender_url %>'><%= activity.sender_full_name %></a>\
          <%= activity.verb %>\
          <% if (activity.sender_id != activity.target_user_id) { %>\
            <a href='<%= activity.target_user_url %>'><%= activity.target_user_full_name %></a>\
            rave about\
          <% } %>\
          <a href='<%= activity.target_url %>'><%= activity.target_title %></a>.\
        </div>\
        <div class='activity-text'>\
          <%= activity.text %>\
        </div>\
        <div class='activity-meta'>\
          <div class='pull-left'>\
            <%= activity.time_since %> ago\
          </div>\
          <div class='pull-right'>\
            <a data-toggle='modal' href='#re-rave'>re-rave</a> &#8226;\
            <a data-toggle='modal' href='#disagree'>disagree</a> &#8226;\
            <a data-toggle='modal' href='#thank'>\
              thank <%= activity.target_user_first_name %>\
            </a>\
          </div>\
          <div style='clear: both;'></div>\
        </div>\
      </li>\
      <% } %>"); 

    // Plugin 
    $.fn.loadActivities = function(url) {
      listSelector = $(this);
      $.get(url, function(activities) {
        var raves = activityTemplate({ activities: activities });
        listSelector.append(raves);
        listSelector.trigger('appended');
      });

      listSelector.bind('appended', function() {
        $('.btn-bookmark').click(function() {
          var button = $(this);
          if ($.trim(button.text()) == 'Add to Scrapbook') {
            $.ajax({
              type: 'POST',
              url: '/bookmark/' + button.val() + '/',
              data: { csrfmiddlewaretoken: CSRF_TOKEN },
              success: function() {
                button.toggleClass('btn-warning').toggleClass('btn-success');
                button.text('Remove It');
              }
            });
          }
          else {
            $.ajax({
              type: 'POST',
              url: '/unbookmark/' + button.val() + '/',
              data: { csrfmiddlewaretoken: CSRF_TOKEN },
              success: function() {
                button.toggleClass('btn-warning').toggleClass('btn-success');
                button.text('Add to Scrapbook');
              }
            });
          }
        });         
      });
    } 
}) (jQuery);
