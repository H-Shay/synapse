# Copyright 2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import urllib.parse
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, TypeVar

import bleach
import jinja2

from synapse.api.constants import EventTypes, Membership
from synapse.api.errors import StoreError
from synapse.config.emailconfig import EmailSubjectConfig
from synapse.events import EventBase
from synapse.push.presentable_names import (
    calculate_room_name,
    descriptor_from_member_events,
    name_from_member_event,
)
from synapse.storage.state import StateFilter
from synapse.types import StateMap, UserID
from synapse.util.async_helpers import concurrently_execute
from synapse.visibility import filter_events_for_client

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

T = TypeVar("T")


CONTEXT_BEFORE = 1
CONTEXT_AFTER = 1

# From https://github.com/matrix-org/matrix-react-sdk/blob/master/src/HtmlUtils.js
ALLOWED_TAGS = [
    "font",  # custom to matrix for IRC-style font coloring
    "del",  # for markdown
    # deliberately no h1/h2 to stop people shouting.
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "p",
    "a",
    "ul",
    "ol",
    "nl",
    "li",
    "b",
    "i",
    "u",
    "strong",
    "em",
    "strike",
    "code",
    "hr",
    "br",
    "div",
    "table",
    "thead",
    "caption",
    "tbody",
    "tr",
    "th",
    "td",
    "pre",
]
ALLOWED_ATTRS = {
    # custom ones first:
    "font": ["color"],  # custom to matrix
    "a": ["href", "name", "target"],  # remote target: custom to matrix
    # We don't currently allow img itself by default, but this
    # would make sense if we did
    "img": ["src"],
}
# When bleach release a version with this option, we can specify schemes
# ALLOWED_SCHEMES = ["http", "https", "ftp", "mailto"]


class Mailer:
    def __init__(
        self,
        hs: "HomeServer",
        app_name: str,
        template_html: jinja2.Template,
        template_text: jinja2.Template,
    ):
        self.hs = hs
        self.template_html = template_html
        self.template_text = template_text

        self.send_email_handler = hs.get_send_email_handler()
        self.store = self.hs.get_datastore()
        self.state_store = self.hs.get_storage().state
        self.macaroon_gen = self.hs.get_macaroon_generator()
        self.state_handler = self.hs.get_state_handler()
        self.storage = hs.get_storage()
        self.app_name = app_name
        self.email_subjects = hs.config.email_subjects  # type: EmailSubjectConfig

        logger.info("Created Mailer for app_name %s" % app_name)

    async def send_password_reset_mail(
        self, email_address: str, token: str, client_secret: str, sid: str
    ) -> None:
        """Send an email with a password reset link to a user

        Args:
            email_address: Email address we're sending the password
                reset to
            token: Unique token generated by the server to verify
                the email was received
            client_secret: Unique token generated by the client to
                group together multiple email sending attempts
            sid: The generated session ID
        """
        params = {"token": token, "client_secret": client_secret, "sid": sid}
        link = (
            self.hs.config.public_baseurl
            + "_synapse/client/password_reset/email/submit_token?%s"
            % urllib.parse.urlencode(params)
        )

        template_vars = {"link": link}

        await self.send_email(
            email_address,
            self.email_subjects.password_reset
            % {"server_name": self.hs.config.server_name},
            template_vars,
        )

    async def send_registration_mail(
        self, email_address: str, token: str, client_secret: str, sid: str
    ) -> None:
        """Send an email with a registration confirmation link to a user

        Args:
            email_address: Email address we're sending the registration
                link to
            token: Unique token generated by the server to verify
                the email was received
            client_secret: Unique token generated by the client to
                group together multiple email sending attempts
            sid: The generated session ID
        """
        params = {"token": token, "client_secret": client_secret, "sid": sid}
        link = (
            self.hs.config.public_baseurl
            + "_matrix/client/unstable/registration/email/submit_token?%s"
            % urllib.parse.urlencode(params)
        )

        template_vars = {"link": link}

        await self.send_email(
            email_address,
            self.email_subjects.email_validation
            % {"server_name": self.hs.config.server_name},
            template_vars,
        )

    async def send_add_threepid_mail(
        self, email_address: str, token: str, client_secret: str, sid: str
    ) -> None:
        """Send an email with a validation link to a user for adding a 3pid to their account

        Args:
            email_address: Email address we're sending the validation link to

            token: Unique token generated by the server to verify the email was received

            client_secret: Unique token generated by the client to group together
                multiple email sending attempts

            sid: The generated session ID
        """
        params = {"token": token, "client_secret": client_secret, "sid": sid}
        link = (
            self.hs.config.public_baseurl
            + "_matrix/client/unstable/add_threepid/email/submit_token?%s"
            % urllib.parse.urlencode(params)
        )

        template_vars = {"link": link}

        await self.send_email(
            email_address,
            self.email_subjects.email_validation
            % {"server_name": self.hs.config.server_name},
            template_vars,
        )

    async def send_notification_mail(
        self,
        app_id: str,
        user_id: str,
        email_address: str,
        push_actions: Iterable[Dict[str, Any]],
        reason: Dict[str, Any],
    ) -> None:
        """
        Send email regarding a user's room notifications

        Params:
            app_id: The application receiving the notification.
            user_id: The user receiving the notification.
            email_address: The email address receiving the notification.
            push_actions: All outstanding notifications.
            reason: The notification that was ready and is the cause of an email
                being sent.
        """
        rooms_in_order = deduped_ordered_list([pa["room_id"] for pa in push_actions])

        notif_events = await self.store.get_events(
            [pa["event_id"] for pa in push_actions]
        )

        notifs_by_room = {}  # type: Dict[str, List[Dict[str, Any]]]
        for pa in push_actions:
            notifs_by_room.setdefault(pa["room_id"], []).append(pa)

        # collect the current state for all the rooms in which we have
        # notifications
        state_by_room = {}

        try:
            user_display_name = await self.store.get_profile_displayname(
                UserID.from_string(user_id).localpart
            )
            if user_display_name is None:
                user_display_name = user_id
        except StoreError:
            user_display_name = user_id

        async def _fetch_room_state(room_id: str) -> None:
            room_state = await self.store.get_current_state_ids(room_id)
            state_by_room[room_id] = room_state

        # Run at most 3 of these at once: sync does 10 at a time but email
        # notifs are much less realtime than sync so we can afford to wait a bit.
        await concurrently_execute(_fetch_room_state, rooms_in_order, 3)

        # actually sort our so-called rooms_in_order list, most recent room first
        rooms_in_order.sort(key=lambda r: -(notifs_by_room[r][-1]["received_ts"] or 0))

        rooms = []

        for r in rooms_in_order:
            roomvars = await self._get_room_vars(
                r, user_id, notifs_by_room[r], notif_events, state_by_room[r]
            )
            rooms.append(roomvars)

        reason["room_name"] = await calculate_room_name(
            self.store,
            state_by_room[reason["room_id"]],
            user_id,
            fallback_to_members=True,
        )

        if len(notifs_by_room) == 1:
            # Only one room has new stuff
            room_id = list(notifs_by_room.keys())[0]

            summary_text = await self._make_summary_text_single_room(
                room_id,
                notifs_by_room[room_id],
                state_by_room[room_id],
                notif_events,
                user_id,
            )
        else:
            summary_text = await self._make_summary_text(
                notifs_by_room, state_by_room, notif_events, reason
            )

        template_vars = {
            "user_display_name": user_display_name,
            "unsubscribe_link": self._make_unsubscribe_link(
                user_id, app_id, email_address
            ),
            "summary_text": summary_text,
            "rooms": rooms,
            "reason": reason,
        }

        await self.send_email(email_address, summary_text, template_vars)

    async def send_email(
        self, email_address: str, subject: str, extra_template_vars: Dict[str, Any]
    ) -> None:
        """Send an email with the given information and template text"""
        template_vars = {
            "app_name": self.app_name,
            "server_name": self.hs.config.server.server_name,
        }

        template_vars.update(extra_template_vars)

        html_text = self.template_html.render(**template_vars)
        plain_text = self.template_text.render(**template_vars)

        await self.send_email_handler.send_email(
            email_address=email_address,
            subject=subject,
            app_name=self.app_name,
            html=html_text,
            text=plain_text,
        )

    async def _get_room_vars(
        self,
        room_id: str,
        user_id: str,
        notifs: Iterable[Dict[str, Any]],
        notif_events: Dict[str, EventBase],
        room_state_ids: StateMap[str],
    ) -> Dict[str, Any]:
        """
        Generate the variables for notifications on a per-room basis.

        Args:
            room_id: The room ID
            user_id: The user receiving the notification.
            notifs: The outstanding push actions for this room.
            notif_events: The events related to the above notifications.
            room_state_ids: The event IDs of the current room state.

        Returns:
             A dictionary to be added to the template context.
        """

        # Check if one of the notifs is an invite event for the user.
        is_invite = False
        for n in notifs:
            ev = notif_events[n["event_id"]]
            if ev.type == EventTypes.Member and ev.state_key == user_id:
                if ev.content.get("membership") == Membership.INVITE:
                    is_invite = True
                    break

        room_name = await calculate_room_name(self.store, room_state_ids, user_id)

        room_vars = {
            "title": room_name,
            "hash": string_ordinal_total(room_id),  # See sender avatar hash
            "notifs": [],
            "invite": is_invite,
            "link": self._make_room_link(room_id),
        }  # type: Dict[str, Any]

        if not is_invite:
            for n in notifs:
                notifvars = await self._get_notif_vars(
                    n, user_id, notif_events[n["event_id"]], room_state_ids
                )

                # merge overlapping notifs together.
                # relies on the notifs being in chronological order.
                merge = False
                if room_vars["notifs"] and "messages" in room_vars["notifs"][-1]:
                    prev_messages = room_vars["notifs"][-1]["messages"]
                    for message in notifvars["messages"]:
                        pm = list(
                            filter(lambda pm: pm["id"] == message["id"], prev_messages)
                        )
                        if pm:
                            if not message["is_historical"]:
                                pm[0]["is_historical"] = False
                            merge = True
                        elif merge:
                            # we're merging, so append any remaining messages
                            # in this notif to the previous one
                            prev_messages.append(message)

                if not merge:
                    room_vars["notifs"].append(notifvars)

        return room_vars

    async def _get_notif_vars(
        self,
        notif: Dict[str, Any],
        user_id: str,
        notif_event: EventBase,
        room_state_ids: StateMap[str],
    ) -> Dict[str, Any]:
        """
        Generate the variables for a single notification.

        Args:
            notif: The outstanding notification for this room.
            user_id: The user receiving the notification.
            notif_event: The event related to the above notification.
            room_state_ids: The event IDs of the current room state.

        Returns:
             A dictionary to be added to the template context.
        """

        results = await self.store.get_events_around(
            notif["room_id"],
            notif["event_id"],
            before_limit=CONTEXT_BEFORE,
            after_limit=CONTEXT_AFTER,
        )

        ret = {
            "link": self._make_notif_link(notif),
            "ts": notif["received_ts"],
            "messages": [],
        }

        the_events = await filter_events_for_client(
            self.storage, user_id, results["events_before"]
        )
        the_events.append(notif_event)

        for event in the_events:
            messagevars = await self._get_message_vars(notif, event, room_state_ids)
            if messagevars is not None:
                ret["messages"].append(messagevars)

        return ret

    async def _get_message_vars(
        self, notif: Dict[str, Any], event: EventBase, room_state_ids: StateMap[str]
    ) -> Optional[Dict[str, Any]]:
        """
        Generate the variables for a single event, if possible.

        Args:
            notif: The outstanding notification for this room.
            event: The event under consideration.
            room_state_ids: The event IDs of the current room state.

        Returns:
             A dictionary to be added to the template context, or None if the
             event cannot be processed.
        """
        if event.type != EventTypes.Message and event.type != EventTypes.Encrypted:
            return None

        # Get the sender's name and avatar from the room state.
        type_state_key = ("m.room.member", event.sender)
        sender_state_event_id = room_state_ids.get(type_state_key)
        if sender_state_event_id:
            sender_state_event = await self.store.get_event(
                sender_state_event_id
            )  # type: Optional[EventBase]
        else:
            # Attempt to check the historical state for the room.
            historical_state = await self.state_store.get_state_for_event(
                event.event_id, StateFilter.from_types((type_state_key,))
            )
            sender_state_event = historical_state.get(type_state_key)

        if sender_state_event:
            sender_name = name_from_member_event(sender_state_event)
            sender_avatar_url = sender_state_event.content.get("avatar_url")
        else:
            # No state could be found, fallback to the MXID.
            sender_name = event.sender
            sender_avatar_url = None

        # 'hash' for deterministically picking default images: use
        # sender_hash % the number of default images to choose from
        sender_hash = string_ordinal_total(event.sender)

        ret = {
            "event_type": event.type,
            "is_historical": event.event_id != notif["event_id"],
            "id": event.event_id,
            "ts": event.origin_server_ts,
            "sender_name": sender_name,
            "sender_avatar_url": sender_avatar_url,
            "sender_hash": sender_hash,
        }

        # Encrypted messages don't have any additional useful information.
        if event.type == EventTypes.Encrypted:
            return ret

        msgtype = event.content.get("msgtype")

        ret["msgtype"] = msgtype

        if msgtype == "m.text":
            self._add_text_message_vars(ret, event)
        elif msgtype == "m.image":
            self._add_image_message_vars(ret, event)

        if "body" in event.content:
            ret["body_text_plain"] = event.content["body"]

        return ret

    def _add_text_message_vars(
        self, messagevars: Dict[str, Any], event: EventBase
    ) -> None:
        """
        Potentially add a sanitised message body to the message variables.

        Args:
            messagevars: The template context to be modified.
            event: The event under consideration.
        """
        msgformat = event.content.get("format")

        messagevars["format"] = msgformat

        formatted_body = event.content.get("formatted_body")
        body = event.content.get("body")

        if msgformat == "org.matrix.custom.html" and formatted_body:
            messagevars["body_text_html"] = safe_markup(formatted_body)
        elif body:
            messagevars["body_text_html"] = safe_text(body)

    def _add_image_message_vars(
        self, messagevars: Dict[str, Any], event: EventBase
    ) -> None:
        """
        Potentially add an image URL to the message variables.

        Args:
            messagevars: The template context to be modified.
            event: The event under consideration.
        """
        if "url" in event.content:
            messagevars["image_url"] = event.content["url"]

    async def _make_summary_text_single_room(
        self,
        room_id: str,
        notifs: List[Dict[str, Any]],
        room_state_ids: StateMap[str],
        notif_events: Dict[str, EventBase],
        user_id: str,
    ) -> str:
        """
        Make a summary text for the email when only a single room has notifications.

        Args:
            room_id: The ID of the room.
            notifs: The push actions for this room.
            room_state_ids: The state map for the room.
            notif_events: A map of event ID -> notification event.
            user_id: The user receiving the notification.

        Returns:
            The summary text.
        """
        # If the room has some kind of name, use it, but we don't
        # want the generated-from-names one here otherwise we'll
        # end up with, "new message from Bob in the Bob room"
        room_name = await calculate_room_name(
            self.store, room_state_ids, user_id, fallback_to_members=False
        )

        # See if one of the notifs is an invite event for the user
        invite_event = None
        for n in notifs:
            ev = notif_events[n["event_id"]]
            if ev.type == EventTypes.Member and ev.state_key == user_id:
                if ev.content.get("membership") == Membership.INVITE:
                    invite_event = ev
                    break

        if invite_event:
            inviter_member_event_id = room_state_ids.get(
                ("m.room.member", invite_event.sender)
            )
            inviter_name = invite_event.sender
            if inviter_member_event_id:
                inviter_member_event = await self.store.get_event(
                    inviter_member_event_id, allow_none=True
                )
                if inviter_member_event:
                    inviter_name = name_from_member_event(inviter_member_event)

            if room_name is None:
                return self.email_subjects.invite_from_person % {
                    "person": inviter_name,
                    "app": self.app_name,
                }

            return self.email_subjects.invite_from_person_to_room % {
                "person": inviter_name,
                "room": room_name,
                "app": self.app_name,
            }

        if len(notifs) == 1:
            # There is just the one notification, so give some detail
            sender_name = None
            event = notif_events[notifs[0]["event_id"]]
            if ("m.room.member", event.sender) in room_state_ids:
                state_event_id = room_state_ids[("m.room.member", event.sender)]
                state_event = await self.store.get_event(state_event_id)
                sender_name = name_from_member_event(state_event)

            if sender_name is not None and room_name is not None:
                return self.email_subjects.message_from_person_in_room % {
                    "person": sender_name,
                    "room": room_name,
                    "app": self.app_name,
                }
            elif sender_name is not None:
                return self.email_subjects.message_from_person % {
                    "person": sender_name,
                    "app": self.app_name,
                }

            # The sender is unknown, just use the room name (or ID).
            return self.email_subjects.messages_in_room % {
                "room": room_name or room_id,
                "app": self.app_name,
            }
        else:
            # There's more than one notification for this room, so just
            # say there are several
            if room_name is not None:
                return self.email_subjects.messages_in_room % {
                    "room": room_name,
                    "app": self.app_name,
                }

            return await self._make_summary_text_from_member_events(
                room_id, notifs, room_state_ids, notif_events
            )

    async def _make_summary_text(
        self,
        notifs_by_room: Dict[str, List[Dict[str, Any]]],
        room_state_ids: Dict[str, StateMap[str]],
        notif_events: Dict[str, EventBase],
        reason: Dict[str, Any],
    ) -> str:
        """
        Make a summary text for the email when multiple rooms have notifications.

        Args:
            notifs_by_room: A map of room ID to the push actions for that room.
            room_state_ids: A map of room ID to the state map for that room.
            notif_events: A map of event ID -> notification event.
            reason: The reason this notification is being sent.

        Returns:
            The summary text.
        """
        # Stuff's happened in multiple different rooms
        # ...but we still refer to the 'reason' room which triggered the mail
        if reason["room_name"] is not None:
            return self.email_subjects.messages_in_room_and_others % {
                "room": reason["room_name"],
                "app": self.app_name,
            }

        room_id = reason["room_id"]
        return await self._make_summary_text_from_member_events(
            room_id, notifs_by_room[room_id], room_state_ids[room_id], notif_events
        )

    async def _make_summary_text_from_member_events(
        self,
        room_id: str,
        notifs: List[Dict[str, Any]],
        room_state_ids: StateMap[str],
        notif_events: Dict[str, EventBase],
    ) -> str:
        """
        Make a summary text for the email when only a single room has notifications.

        Args:
            room_id: The ID of the room.
            notifs: The push actions for this room.
            room_state_ids: The state map for the room.
            notif_events: A map of event ID -> notification event.

        Returns:
            The summary text.
        """
        # If the room doesn't have a name, say who the messages
        # are from explicitly to avoid, "messages in the Bob room"

        # Find the latest event ID for each sender, note that the notifications
        # are already in descending received_ts.
        sender_ids = {}
        for n in notifs:
            sender = notif_events[n["event_id"]].sender
            if sender not in sender_ids:
                sender_ids[sender] = n["event_id"]

        # Get the actual member events (in order to calculate a pretty name for
        # the room).
        member_event_ids = []
        member_events = {}
        for sender_id, event_id in sender_ids.items():
            type_state_key = ("m.room.member", sender_id)
            sender_state_event_id = room_state_ids.get(type_state_key)
            if sender_state_event_id:
                member_event_ids.append(sender_state_event_id)
            else:
                # Attempt to check the historical state for the room.
                historical_state = await self.state_store.get_state_for_event(
                    event_id, StateFilter.from_types((type_state_key,))
                )
                sender_state_event = historical_state.get(type_state_key)
                if sender_state_event:
                    member_events[event_id] = sender_state_event
        member_events.update(await self.store.get_events(member_event_ids))

        if not member_events:
            # No member events were found! Maybe the room is empty?
            # Fallback to the room ID (note that if there was a room name this
            # would already have been used previously).
            return self.email_subjects.messages_in_room % {
                "room": room_id,
                "app": self.app_name,
            }

        # There was a single sender.
        if len(member_events) == 1:
            return self.email_subjects.messages_from_person % {
                "person": descriptor_from_member_events(member_events.values()),
                "app": self.app_name,
            }

        # There was more than one sender, use the first one and a tweaked template.
        return self.email_subjects.messages_from_person_and_others % {
            "person": descriptor_from_member_events(list(member_events.values())[:1]),
            "app": self.app_name,
        }

    def _make_room_link(self, room_id: str) -> str:
        """
        Generate a link to open a room in the web client.

        Args:
            room_id: The room ID to generate a link to.

        Returns:
             A link to open a room in the web client.
        """
        if self.hs.config.email_riot_base_url:
            base_url = "%s/#/room" % (self.hs.config.email_riot_base_url)
        elif self.app_name == "Vector":
            # need /beta for Universal Links to work on iOS
            base_url = "https://vector.im/beta/#/room"
        else:
            base_url = "https://matrix.to/#"
        return "%s/%s" % (base_url, room_id)

    def _make_notif_link(self, notif: Dict[str, str]) -> str:
        """
        Generate a link to open an event in the web client.

        Args:
            notif: The notification to generate a link for.

        Returns:
             A link to open the notification in the web client.
        """
        if self.hs.config.email_riot_base_url:
            return "%s/#/room/%s/%s" % (
                self.hs.config.email_riot_base_url,
                notif["room_id"],
                notif["event_id"],
            )
        elif self.app_name == "Vector":
            # need /beta for Universal Links to work on iOS
            return "https://vector.im/beta/#/room/%s/%s" % (
                notif["room_id"],
                notif["event_id"],
            )
        else:
            return "https://matrix.to/#/%s/%s" % (notif["room_id"], notif["event_id"])

    def _make_unsubscribe_link(
        self, user_id: str, app_id: str, email_address: str
    ) -> str:
        """
        Generate a link to unsubscribe from email notifications.

        Args:
            user_id: The user receiving the notification.
            app_id: The application receiving the notification.
            email_address: The email address receiving the notification.

        Returns:
             A link to unsubscribe from email notifications.
        """
        params = {
            "access_token": self.macaroon_gen.generate_delete_pusher_token(user_id),
            "app_id": app_id,
            "pushkey": email_address,
        }

        # XXX: make r0 once API is stable
        return "%s_matrix/client/unstable/pushers/remove?%s" % (
            self.hs.config.public_baseurl,
            urllib.parse.urlencode(params),
        )


def safe_markup(raw_html: str) -> jinja2.Markup:
    """
    Sanitise a raw HTML string to a set of allowed tags and attributes, and linkify any bare URLs.

    Args
        raw_html: Unsafe HTML.

    Returns:
        A Markup object ready to safely use in a Jinja template.
    """
    return jinja2.Markup(
        bleach.linkify(
            bleach.clean(
                raw_html,
                tags=ALLOWED_TAGS,
                attributes=ALLOWED_ATTRS,
                # bleach master has this, but it isn't released yet
                # protocols=ALLOWED_SCHEMES,
                strip=True,
            )
        )
    )


def safe_text(raw_text: str) -> jinja2.Markup:
    """
    Sanitise text (escape any HTML tags), and then linkify any bare URLs.

    Args
        raw_text: Unsafe text which might include HTML markup.

    Returns:
        A Markup object ready to safely use in a Jinja template.
    """
    return jinja2.Markup(
        bleach.linkify(bleach.clean(raw_text, tags=[], attributes={}, strip=False))
    )


def deduped_ordered_list(it: Iterable[T]) -> List[T]:
    seen = set()
    ret = []
    for item in it:
        if item not in seen:
            seen.add(item)
            ret.append(item)
    return ret


def string_ordinal_total(s: str) -> int:
    tot = 0
    for c in s:
        tot += ord(c)
    return tot