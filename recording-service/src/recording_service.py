import asyncio
import logging
from typing import Any, Optional

from pendulum import DateTime, Duration  # type: ignore

from src import audio_storage, ffmpeg_recorder, utils
from src.audio_storage import AudioStorageAdapter
from src.models import RecordingTask

logger = logging.getLogger(__name__)


class RecordAudioService:
    def __init__(
        self,
        audio_storage_adapter: AudioStorageAdapter,
        time_provider: utils.TimeProvider,
    ) -> None:
        super().__init__()
        self._audio_storage_adapter = audio_storage_adapter
        self._time_provider = time_provider

    # Records audio for a given task using ffmpeg
    async def record_audio_task(
        self, task: RecordingTask, metadata: dict[str, Any], run_id: Optional[str] = None
    ):
        current_time = self._time_provider.get_current_time()

        # Account for task being in the future
        await self.wait_until_start_if_in_future(task, current_time)

        # Account for starting in between the recording period
        duration_left = self.get_duration_left(task, current_time)

        # Ensure output directory exists with metadata
        audio_storage.ensure_dir_with_metadata(task.file_path.parent, metadata=metadata)

        logger.info(
            f"Starting recording for task: {task.title}. Duration: {duration_left}. "
            f"URL: {task.stream_url}. Writing to: {task.file_path}"
        )

        await ffmpeg_recorder.record(
            run_id=run_id or str(task.id),
            url=str(task.stream_url),
            duration_seconds=duration_left.in_seconds(),
            output_path=task.file_path,
        )
        logger.info(f"Recording complete: {task.title}")

    def get_duration_left(self, task: RecordingTask, current_time: DateTime):
        duration_left = task.recording_period.get_time_remaining(current_time)
        if duration_left.in_seconds() <= 0:
            raise ValueError(
                f"Task '{task.title}': Recording start time {task.recording_period.start} "
                f"is later than current time {current_time}"
            )
        return duration_left

    # Waits until the start time of the task if it is in the future
    async def wait_until_start_if_in_future(
        self, task: RecordingTask, current_time: DateTime
    ):
        until_start = task.recording_period.get_time_until_start(current_time)
        if until_start.in_seconds() > 0:
            logger.info(
                f"Task '{task.title}': Waiting until start time {task.recording_period.start} (in {until_start})"
            )
            await asyncio.sleep(until_start.in_seconds())
