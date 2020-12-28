import asyncio
import json
from asgiref.sync import async_to_sync
from channels.consumer import AsyncConsumer
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async
from channels.auth import login
from .models import User, PartySession, UserJoinedPartySession, Song, UserPlaylist


class ChatConsumer(AsyncConsumer):
    async def websocket_connect(self, event):

        # @Moritz authentication here
        self.user = self.scope["user"]
        self.room_name = self.scope['url_route']['kwargs']['room_name']

        print("connected", event)

        await self.user_join_party_session(self.user, await self.get_current_party_session(self.room_name))

        print("Current room name: " + self.room_name)
        self.room_group_name = 'mytest_%s' % self.room_name
        print("Current group name: " + self.room_group_name)
        self.user_id = await self.get_user_id()
        print('User-ID: ' + self.user_id)

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.send({
            'type': 'websocket.accept'
        })
        await self.send({
            "type": "websocket.send",
            "text": "Connected to group: " + self.room_group_name
        })

    # differentiate client messages here
    async def websocket_receive(self, event):
        if str(event.get('text')) == 'Timer':
            asyncio.create_task(self.timer_task())
        elif str(event.get('text')) == 'start_party_session' and await self.user_is_session_host(self.user, await self.get_current_party_session(self.room_name)):
            asyncio.create_task(self.init_session_task())
        elif 'button' in str(event.get('text')):
            asyncio.create_task(self.voting_task(event))
        else:
            asyncio.create_task(self.new_vote_task(event))

    async def timer_task(self):
        countdown = 5
        while True:
            countdown -= 1
            # @Moritz send current countdown to database here
            await self.countdown_to_database(countdown)
            await self.channel_layer.group_send(
                self.room_group_name, {
                    "type": "voting_timer",
                    "text": str(countdown)
                }
            )
            if countdown == 0:
                # @Moritz check voted values in database and reset values
                await self.receive_values_database()
                break
            await asyncio.sleep(1)

    async def init_session_task(self):
        playing_song = await self.get_playing_song(await self.get_user_playlist(await self.get_current_party_session(self.room_name)))
        votable_songs = await self.get_votable_songs(await self.get_user_playlist(await self.get_current_party_session(self.room_name)))

        init_data = {
            "type": "session_init",
            "playing_song": playing_song,
            "votable_songs": votable_songs
        }

        await self.channel_layer.group_send(
            self.room_group_name, {
                "type": "session_init",
                "text": str(init_data).replace("'", '"')
            }
        )

    async def new_vote_task(self, event):
        self.new_vote = str(event.get("text"))

        prev_vote_id = await self.get_current_user_vote(await self.get_user_join_party_session(self.user, await self.get_current_party_session(self.room_name)))
        print("vote for " + self.new_vote + "; previous vote was: " + str(prev_vote_id))

        voted_song = await self.get_song_by_spotify_id(self.new_vote, await self.get_user_playlist(await self.get_current_party_session(self.room_name)))

        if prev_vote_id:
            prev_voted_song = await self.get_song_by_spotify_id(prev_vote_id, await self.get_user_playlist(await self.get_current_party_session(self.room_name)))
        else:
            prev_voted_song = None

        if voted_song and await self.check_song_votable(voted_song):
            if voted_song != prev_voted_song:
                await self.set_user_vote(voted_song, await self.get_user_join_party_session(self.user, await self.get_current_party_session(self.room_name)))
                await self.change_song_votes(voted_song, 1)
                if prev_voted_song:
                    await self.change_song_votes(prev_voted_song, -1)

                asyncio.create_task(self.refresh_votes_task())
            else:
                print("song already voted for")
        else:
            print("song not votable or doesn't exist")

    async def refresh_votes_task(self):
        votable_songs = await self.get_votable_songs(await self.get_user_playlist(await self.get_current_party_session(self.room_name)))

        refresh_data = {
            "type": "votes_refresh",
            "votable_songs": votable_songs
        }

        await self.channel_layer.group_send(
            self.room_group_name, {
                "type": "votes_refresh",
                "text": str(refresh_data).replace("'", '"')
            }
        )

    async def voting_task(self, event):
        raw_json = event.get("text")
        data_json = json.loads(raw_json)
        vote_value = int(data_json["button_val"]) + 1
        button_id = data_json["button"]
        print(f'{button_id} and {vote_value}')
        # @Moritz sending button_id, value and user here
        user = "test"
        await self.add_vote_to_database(button_id, vote_value, user)

        # Updating json with new vote_value
        data_json["button_val"] = str(vote_value)
        send_to_js = str(data_json).replace("'", '"')
        await self.channel_layer.group_send(
            self.room_group_name, {
                "type": "voting_count",
                "text": send_to_js
            }
        )

    # wrapper functions for websocket send
    async def voting_count(self, event):
        await self.send({
            "type": "websocket.send",
            "text": event['text']
        })

    async def session_init(self, event):
        await self.send({
            "type": "websocket.send",
            "text": event['text']
        })

    async def votes_refresh(self, event):
        await self.send({
            "type": "websocket.send",
            "text": event['text']
        })

    async def voting_timer(self, event):
        await self.send({
            "type": "websocket.send",
            "text": event['text']
        })

    async def force_disconnect(self, event):
        await self.send({
            "type": "websocket.close"
        })

    async def websocket_disconnect(self, event):
        # close websocket for all users in session and delete session model CASCADE
        if await self.get_current_party_session(self.room_name) and await self.user_is_session_host(self.user, await self.get_current_party_session(self.room_name)):
            print("disconnected Host-User: " + self.user_id + "disconnecting all users and deleting session", event)
            await self.delete_current_session(await self.get_current_party_session(self.room_name))
            await self.channel_layer.group_send(
                self.room_group_name, {
                    "type": "force_disconnect"
                }
            )
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
        else:
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
            if await self.get_current_party_session(self.room_name):
                await self.user_leave_party_session(self.user, await self.get_current_party_session(self.room_name))
            print("disconnected User: " + self.user_id, event)

    @database_sync_to_async
    def get_user_join_party_session(self, user, party_session):
        return UserJoinedPartySession.objects.filter(user=user, party_session=party_session)[0]

    @database_sync_to_async
    def user_join_party_session(self, user, party_session):
        if not UserJoinedPartySession.objects.filter(user=user, party_session=party_session):
            new_user_joined_party_session = UserJoinedPartySession(user=user, party_session=party_session)
            new_user_joined_party_session.save()

    @database_sync_to_async
    def user_leave_party_session(self, user, party_session):
        UserJoinedPartySession.objects.filter(user=user, party_session=party_session).delete()

    @database_sync_to_async
    def get_current_party_session(self, current_session_code):
        if PartySession.objects.filter(session_code=current_session_code).exists():
            current_party_session = PartySession.objects.filter(session_code=current_session_code)[0]
            return current_party_session
        else:
            return False

    @database_sync_to_async
    def get_user_id(self):
        return self.user.identifier

    @database_sync_to_async
    def user_is_session_host(self, user, party_session):
        return UserJoinedPartySession.objects.filter(user=user, party_session=party_session)[0].is_session_host

    @database_sync_to_async
    def get_playing_song(self, current_playlist):
        playing_song = Song.objects.filter(user_playlist=current_playlist, is_playing=True)
        return {
            "title_and_artist": playing_song[0].song_name + " - " + playing_song[0].song_artist,
            "length": playing_song[0].song_length,
            "song_id": playing_song[0].spotify_song_id
        }

    @database_sync_to_async
    def get_votable_songs(self, current_playlist):
        votable_songs = Song.objects.filter(user_playlist=current_playlist, is_votable=True)

        votable_songs_arr = []

        for song in votable_songs:
            votable_songs_arr.append(
                {
                    "title_and_artist": song.song_name + " - " + song.song_artist,
                    "length": song.song_length,
                    "votes": song.song_votes,
                    "song_id": song.spotify_song_id
                }
            )

        return votable_songs_arr

    @database_sync_to_async
    def check_song_votable(self, song):
        return song.is_votable

    @database_sync_to_async
    def get_song_by_spotify_id(self, spotify_id, current_playlist):
        if Song.objects.filter(spotify_song_id=spotify_id, user_playlist=current_playlist).exists():
            return Song.objects.filter(spotify_song_id=spotify_id, user_playlist=current_playlist)[0]
        else:
            return False

    @database_sync_to_async
    def get_user_playlist(self, party_session):
        user_playlist = UserPlaylist.objects.filter(is_selected=True, party_session=party_session)[0]
        return user_playlist

    @database_sync_to_async
    def delete_current_session(self, party_session):
        current_session = party_session
        current_session.delete()

    @database_sync_to_async
    def get_current_user_vote(self, user_in_party_session):
        if user_in_party_session.user_vote:
            user_vote_id = user_in_party_session.user_vote.spotify_song_id
            return user_vote_id
        else:
            return None

    @database_sync_to_async
    def set_user_vote(self, voted_song, user_in_party_session):
        user_in_party_session.user_vote = voted_song
        user_in_party_session.save()

    @database_sync_to_async
    def change_song_votes(self, selected_song, vote_amount):
        selected_song.song_votes = selected_song.song_votes + vote_amount
        selected_song.save()

    @database_sync_to_async
    def add_vote_to_database(self, button_id, vote_value, user):
        pass

    @database_sync_to_async
    def receive_values_database(self):
        pass

    @database_sync_to_async
    def countdown_to_database(self, countdown):
        pass
