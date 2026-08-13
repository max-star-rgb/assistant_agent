from assistant_agent.media.video.video_context import SQLiteVideoContextStore, VideoFrame


def test_sqlite_video_context_is_shared_by_independent_resource_owners(tmp_path) -> None:
    path = tmp_path / "video-context.sqlite3"
    ingress_store = SQLiteVideoContextStore(path, window_size=2)
    worker_store = SQLiteVideoContextStore(path, window_size=2)

    for sequence in range(1, 4):
        ingress_store.append_frame(
            VideoFrame(
                video_id="video-1",
                frame_id=f"frame-{sequence}",
                uri=f"/frames/{sequence}.jpg",
                sequence=sequence,
                timestamp_ms=sequence * 100,
                metadata={"source": "agent_service"},
                fingerprint=(sequence, sequence + 1),
                fingerprint_width=2,
                fingerprint_height=1,
            )
        )

    frames = worker_store.get_recent_frames("video-1")

    assert [frame.sequence for frame in frames] == [2, 3]
    assert frames[-1].metadata == {"source": "agent_service"}
    assert frames[-1].fingerprint == (3, 4)


def test_sqlite_video_context_removal_is_visible_to_other_owner(tmp_path) -> None:
    path = tmp_path / "video-context.sqlite3"
    ingress_store = SQLiteVideoContextStore(path)
    worker_store = SQLiteVideoContextStore(path)
    ingress_store.append_frame(
        VideoFrame(
            video_id="video-1",
            frame_id="frame-1",
            uri="/frames/1.jpg",
            sequence=1,
        )
    )

    removed = worker_store.remove_video("video-1")

    assert [frame.frame_id for frame in removed] == ["frame-1"]
    assert ingress_store.get_recent_frames("video-1") == []
