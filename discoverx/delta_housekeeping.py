from typing import Iterable, Callable
from datetime import datetime, timezone
import pandas as pd

from discoverx.table_info import TableInfo

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.window import Window
import pyspark.sql.types as T
import pyspark.sql.functions as F


class DeltaHousekeeping:
    
    def __init__(self, spark: SparkSession) -> None:
        self._spark = spark
        self.empty_schema = T.StructType([
            T.StructField("catalog", T.StringType()),
            T.StructField("database", T.StringType()),
            T.StructField("tableName", T.StringType()),
        ])

    @staticmethod
    def _process_describe_history(
        describe_detail_df: DataFrame, describe_history_df: DataFrame
    ) -> pd.DataFrame:
        """
        processes the DESCRIBE HISTORY result of potentially several tables in different schemas/catalogs
        Provides
        - table stats (size and number of files)
        - timestamp for last & second last OPTIMIZE
        - stats of OPTIMIZE (including ZORDER)
        - timestamp for last & second last VACUUM

        returns a pandas DataFrame, and converts Spark internal dfs to pandas as soon as they are manageable
        the reason being that DESCRIBE HISTORY / DESCRIBE DETAIL cannot be cached
        """
        if not "operation" in describe_history_df.columns:
            return describe_detail_df.toPandas()

        # window over operation
        operation_order = (
            describe_history_df
            .filter(F.col("operation").isin(["OPTIMIZE", "VACUUM END"]))
            .withColumn("operation_order", F.row_number().over(
                Window.partitionBy(["catalog", "database", "tableName", "operation"]).orderBy(F.col("timestamp").desc())
            ))
        )

        if operation_order.isEmpty():
            return describe_detail_df.toPandas()

        operation_order = operation_order.toPandas()

        # max & 2nd timestamp of OPTIMIZE into output
        out = describe_detail_df.toPandas().merge(
            operation_order[(operation_order.operation == "OPTIMIZE") & (operation_order.operation_order == 1)]
            .loc[:, ["catalog", "database", "tableName", "timestamp"]]
            .rename(columns={'timestamp': 'max_optimize_timestamp'}),
            how="outer", on=["catalog", "database", "tableName"]
        )
        out = out.merge(
            operation_order[(operation_order.operation == "OPTIMIZE") & (operation_order.operation_order == 2)]
            .loc[:, ["catalog", "database", "tableName", "timestamp"]]
            .rename(columns={'timestamp': '2nd_optimize_timestamp'}),
            how="outer", on=["catalog", "database", "tableName"]
        )
        # max timestamp of VACUUM into output
        out = out.merge(
            operation_order[(operation_order.operation == "VACUUM END") & (operation_order.operation_order == 1)]
            .loc[:, ["catalog", "database", "tableName", "timestamp"]]
            .rename(columns={'timestamp': 'max_vacuum_timestamp'}),
            how="outer", on=["catalog", "database", "tableName"]
        )
        out = out.merge(
            operation_order[(operation_order.operation == "VACUUM END") & (operation_order.operation_order == 2)]
            .loc[:, ["catalog", "database", "tableName", "timestamp"]]
            .rename(columns={'timestamp': '2nd_vacuum_timestamp'}),
            how="outer", on=["catalog", "database", "tableName"]
        )
        # summary of table metrics
        table_metrics_1 = (
            operation_order[(operation_order['operation'] == 'OPTIMIZE') & (operation_order['operation_order'] == 1)]
            .loc[:, ['catalog', 'database', 'tableName', 'min_file_size', 'p50_file_size', 'max_file_size', 'z_order_by']]
        )

        # write to output
        out = out.merge(
            table_metrics_1,
            how="outer", on=["catalog", "database", "tableName"]
        )

        return out

    @staticmethod
    def save_as_table(
        result: DataFrame,
        housekeeping_table_name: str,
    ):
        """
        Static method to store intermediate results of the scan operation into Delta
        Would make sense only if using map_chunked from the `DataExplorer` object
        (otherwise tables are writen one by one into Delta with overhead)
        """
        (
            result
            .write
            .format("delta")
            .mode("append")
            .option("mergeSchema", "true")
            .saveAsTable(housekeeping_table_name)
        )

    def get_describe_detail(self, table_info: TableInfo):
        dd = self._spark.sql(f"""
            DESCRIBE DETAIL {table_info.catalog}.{table_info.schema}.{table_info.table};
        """)
        dd = (
            dd
            .withColumn("split", F.split(F.col('name'), '\.'))
            .withColumn("catalog", F.col("split").getItem(0))
            .withColumn("database", F.col("split").getItem(1))
            .withColumn("tableName", F.col("split").getItem(2))
            .select([
                F.col("catalog"),
                F.col("database"),
                F.col("tableName"),
                F.col("numFiles").alias("number_of_files"),
                F.col("sizeInBytes").alias("bytes"),
            ])
        )
        return dd

    @staticmethod
    def get_describe_history_statement(table_info: TableInfo):
        return f"""
                    SELECT 
                    '{table_info.catalog}' AS catalog,
                    '{table_info.schema}' AS database, 
                    '{table_info.table}' AS tableName, 
                    operation,
                    timestamp,
                    operationMetrics.minFileSize AS min_file_size,
                    operationMetrics.p50FileSize AS p50_file_size,
                    operationMetrics.maxFileSize AS max_file_size, 
                    operationParameters.zOrderBy AS z_order_by 
                    FROM (DESCRIBE HISTORY {table_info.catalog}.{table_info.schema}.{table_info.table})
                    WHERE operation in ('OPTIMIZE', 'VACUUM END')
                """

    def scan(
        self,
        table_info: TableInfo,
    ) -> pd.DataFrame:
        """
        Scans a table_info to fetch Delta stats
        - DESCRIBE DETAIL
        - DESCRIBE HISTORY
        """
        try:
            # runs a describe detail per table, figures out if exception
            dd = self.get_describe_detail(table_info)

            # prepares a DESCRIBE HISTORY statement per table (will be run outside the try-catch)
            statement = self.get_describe_history_statement(table_info)

            return self._process_describe_history(
                dd,
                self._spark.sql(statement),
            )

        except Exception as e:
            errors_df = self._spark.createDataFrame(
                [(table_info.catalog or "", table_info.schema, table_info.table, str(e))],
                ["catalog", "database", "tableName", "error"]
            )
            return errors_df.toPandas()


class DeltaHousekeepingActions:
    """
    Processes the output of the `DeltaHousekeeping` object to provide recommendations
    - tables that need to be OPTIMIZED/VACUUM'ed
    - are tables OPTIMIZED/VACUUM'ed often enough
    - tables that have small files / tables for which ZORDER is not being effective
    """

    def __init__(
        self,
        mapped_pd_dfs: Iterable[pd.DataFrame],
        spark: SparkSession = None,
        min_table_size_optimize: int = 128*1024*1024,  # i.e. 128 MB
        min_days_not_optimized: int = 7,  # in days
        min_days_not_vacuumed: int = 31,  # in days
        max_optimize_freq: int = 2,  # in days - e.g. 2 means that a daily run would be flagged
        max_vacuum_freq: int = 2,  # in days - e.g. 2 means that a daily run would be flagged
        small_file_threshold: int = 32*1024*1024,  # i.e. 32 MB
        min_number_of_files_for_zorder: int = 8,
        stats: pd.DataFrame = None,  # for testability only
    ) -> None:
        if stats is None:
            self._mapped_pd_dfs = mapped_pd_dfs
            stats = pd.concat(self._mapped_pd_dfs)
        self._stats: pd.DataFrame = stats
        
        if spark is None:
            spark = SparkSession.builder.getOrCreate()
        self._spark = spark
        
        self.min_table_size_optimize = min_table_size_optimize
        self.min_days_not_optimized = min_days_not_optimized
        self.min_days_not_vacuumed = min_days_not_vacuumed
        self.max_optimize_freq = max_optimize_freq
        self.max_vacuum_freq = max_vacuum_freq
        self.small_file_threshold = small_file_threshold
        self.min_number_of_files_for_zorder = min_number_of_files_for_zorder
        self.tables_not_optimized_legend = "The table has not been OPTIMIZED and would benefit from it"
        self.tables_not_vacuumed_legend = "The table has never been VACUUM'ed"
        self.tables_not_optimized_last_days = "Tables that are not OPTIMIZED often enough"
        self.tables_not_vacuumed_last_days = "Tables that are not VACUUM'ed often enough"
        self.tables_optimized_too_freq = "Tables that are OPTIMIZED too often"
        self.tables_vacuumed_too_freq = "Tables that are VACUUM'ed too often"
        self.tables_do_not_need_optimize = "Tables that are too small to be OPTIMIZED"
        self.tables_to_analyze = "Tables that need more analysis -small_files"
        self.tables_zorder_not_effective = "Tables for which ZORDER is not being effective"

    def _apply_changes_to_stats(
        self,
        condition: pd.Series,
        boolean_column_name: str,
        reason_column_name: str,
        f_apply_legend: Callable,
        **kwargs
    ) -> pd.DataFrame:
        compose_results = False
        boolean_column_name_new = boolean_column_name
        reason_column_name_new = reason_column_name
        if boolean_column_name in self._stats.columns:
            compose_results = True
            boolean_column_name_new = boolean_column_name + "_new"
            reason_column_name_new = reason_column_name + "_new"

        stats = self._stats.copy()
        stats[boolean_column_name_new] = False
        stats[reason_column_name_new] = None
        stats_sub = stats.loc[condition]
        stats_sub = f_apply_legend(stats_sub.copy(), boolean_column_name_new, reason_column_name_new, **kwargs)
        self._stats = pd.merge(
            self._stats,
            stats_sub.loc[:, ["catalog", "database", "tableName", boolean_column_name_new, reason_column_name_new]],
            on=["catalog", "database", "tableName"],
            how="outer",
        )
        self._stats = self._stats.fillna({boolean_column_name: False, reason_column_name: ""})
        if compose_results:
            self._stats = self._stats.fillna({boolean_column_name_new: False, reason_column_name_new: ""})
            self._stats.loc[:, boolean_column_name] = \
                self._stats[boolean_column_name] | self._stats[boolean_column_name_new]
            self._stats.loc[:, reason_column_name] = \
                self._stats[[reason_column_name, reason_column_name_new]].agg(' | '.join, axis=1)  # TODO should figure out if either side is None
            self._stats.drop([boolean_column_name_new, reason_column_name_new], axis=1, inplace=True)

    def _need_optimize(self) -> pd.DataFrame:
        def check_min_table_size_apply_legend(stats_sub, boolean_column_name, reason_column_name):
            condition2 = stats_sub.bytes.astype(int) > self.min_table_size_optimize
            stats_sub.loc[condition2, boolean_column_name] = True
            stats_sub.loc[condition2, reason_column_name] = self.tables_not_optimized_legend
            return stats_sub

        self._apply_changes_to_stats(
            condition=self._stats.max_optimize_timestamp.isnull() & self._stats.bytes.notnull(),
            boolean_column_name="rec_optimize",
            reason_column_name="rec_optimize_reason",
            f_apply_legend=check_min_table_size_apply_legend,
        )

    def _optimize_not_needed(self) -> pd.DataFrame:
        def check_min_table_size_apply_legend(stats_sub, boolean_column_name, reason_column_name):
            condition2 = stats_sub.max_optimize_timestamp.notnull() & (stats_sub.bytes.astype(int) < self.min_table_size_optimize)
            stats_sub.loc[condition2, boolean_column_name] = True
            stats_sub.loc[condition2, reason_column_name] = self.tables_do_not_need_optimize
            return stats_sub

        self._apply_changes_to_stats(
            condition=self._stats.max_optimize_timestamp.notnull() & self._stats.bytes.notnull(),
            boolean_column_name="rec_optimize",
            reason_column_name="rec_optimize_reason",
            f_apply_legend=check_min_table_size_apply_legend,
        )

    @staticmethod
    def check_timestamps_apply_legend(
        stats_sub, boolean_column_name, reason_column_name, **kwargs,
    ):
        stats_sub.loc[:, kwargs["timestamp_to_evaluate"]] = pd.to_datetime(stats_sub[kwargs["timestamp_to_evaluate"]], utc=True)
        stats_sub.loc[:, 'lag'] = (
            datetime.now(timezone.utc) - stats_sub[kwargs["timestamp_to_evaluate"]]
        ).dt.days
        condition2 = stats_sub['lag'] > kwargs["threshold"]
        stats_sub.loc[condition2, boolean_column_name] = True
        stats_sub.loc[condition2, reason_column_name] = kwargs["reason"]
        return stats_sub

    def _not_optimized_last_days(self) -> pd.DataFrame:
        self._apply_changes_to_stats(
            condition=~self._stats.max_optimize_timestamp.isnull(),
            boolean_column_name="rec_optimize",
            reason_column_name="rec_optimize_reason",
            f_apply_legend=self.check_timestamps_apply_legend,
            timestamp_to_evaluate="max_optimize_timestamp",
            threshold=self.min_days_not_optimized,
            reason=self.tables_not_optimized_last_days,
        )

    @staticmethod
    def check_timestamp_diff_apply_legend(
        stats_sub, boolean_column_name, reason_column_name, **kwargs,
    ):
        stats_sub.loc[:, kwargs["timestamp1_to_evaluate"]] = pd.to_datetime(stats_sub[kwargs["timestamp1_to_evaluate"]], utc=True)
        stats_sub.loc[:, kwargs["timestamp2_to_evaluate"]] = pd.to_datetime(stats_sub[kwargs["timestamp2_to_evaluate"]], utc=True)
        stats_sub.loc[:, 'lag'] = (
            stats_sub[kwargs["timestamp1_to_evaluate"]] - stats_sub[kwargs["timestamp2_to_evaluate"]]
        ).dt.days
        condition2 = stats_sub['lag'] < kwargs["threshold"]
        stats_sub.loc[condition2, boolean_column_name] = True
        stats_sub.loc[condition2, reason_column_name] = kwargs["reason"]
        return stats_sub

    def _optimized_too_frequently(self) -> pd.DataFrame:
        self._apply_changes_to_stats(
            condition=self._stats.max_optimize_timestamp.notnull() & self._stats["2nd_optimize_timestamp"].notnull(),
            boolean_column_name="rec_optimize",
            reason_column_name="rec_optimize_reason",
            f_apply_legend=self.check_timestamp_diff_apply_legend,
            timestamp1_to_evaluate="max_optimize_timestamp",
            timestamp2_to_evaluate="2nd_optimize_timestamp",
            threshold=self.max_optimize_freq,
            reason=self.tables_optimized_too_freq,
        )

    def _never_vacuumed(self) -> pd.DataFrame:
        def apply_legend(stats_sub, boolean_column_name, reason_column_name):
            stats_sub.loc[:, boolean_column_name] = True
            stats_sub.loc[:, reason_column_name] = self.tables_not_vacuumed_legend
            return stats_sub

        self._apply_changes_to_stats(
            condition=self._stats.max_vacuum_timestamp.isnull(),
            boolean_column_name="rec_vacuum",
            reason_column_name="rec_vacuum_reason",
            f_apply_legend=apply_legend,
        )

    def _not_vacuumed_last_days(self) -> pd.DataFrame:
        self._apply_changes_to_stats(
            condition=~self._stats.max_vacuum_timestamp.isnull(),
            boolean_column_name="rec_vacuum",
            reason_column_name="rec_vacuum_reason",
            f_apply_legend=self.check_timestamps_apply_legend,
            timestamp_to_evaluate="max_vacuum_timestamp",
            threshold=self.min_days_not_vacuumed,
            reason=self.tables_not_vacuumed_last_days,
        )
        stats = self._stats.copy()
        stats['max_vacuum_timestamp'] = pd.to_datetime(stats['max_vacuum_timestamp'], utc=True)
        stats['vacuum_lag'] = (
            datetime.now(timezone.utc) - stats['max_vacuum_timestamp']
        ).dt.days
        return (
            stats[stats['vacuum_lag'] < self.min_days_not_vacuumed]
        )

    def _vacuumed_too_frequently(self) -> pd.DataFrame:
        self._apply_changes_to_stats(
            condition=self._stats.max_vacuum_timestamp.notnull() & self._stats["2nd_vacuum_timestamp"].notnull(),
            boolean_column_name="rec_vacuum",
            reason_column_name="rec_vacuum_reason",
            f_apply_legend=self.check_timestamp_diff_apply_legend,
            timestamp1_to_evaluate="max_vacuum_timestamp",
            timestamp2_to_evaluate="2nd_vacuum_timestamp",
            threshold=self.max_vacuum_freq,
            reason=self.tables_vacuumed_too_freq,
        )

    def _analyze_these_tables(self) -> pd.DataFrame:
        def check_analyze_tables_apply_legend(stats_sub, boolean_column_name, reason_column_name):
            condition2 = stats_sub['p50_file_size'].astype(int) < self.small_file_threshold
            stats_sub.loc[condition2, boolean_column_name] = True
            stats_sub.loc[condition2, reason_column_name] = self.tables_to_analyze
            return stats_sub

        self._apply_changes_to_stats(
            condition=self._stats.max_optimize_timestamp.notnull() & self._stats.p50_file_size.notnull() & (self._stats.number_of_files > 1),
            boolean_column_name="rec_misc",
            reason_column_name="rec_misc_reason",
            f_apply_legend=check_analyze_tables_apply_legend,
        )

    def _zorder_not_effective(self) -> pd.DataFrame:
        def check_zorder_not_effective_apply_legend(stats_sub, boolean_column_name, reason_column_name):
            stats_sub['z_order_by_clean'] = stats_sub['z_order_by'].apply(
                lambda x: None if x == "[]" else x.replace('[', '').replace(']', '').replace('"', ''))
            stats_sub['z_order_by_array'] = stats_sub['z_order_by_clean'].str.split(',')

            stats_sub = stats_sub.loc[stats_sub['z_order_by_array'].str.len() > 0, :]
            stats_sub = stats_sub.loc[stats_sub['number_of_files'].astype(int) < self.min_number_of_files_for_zorder, :]

            stats_sub.loc[:, boolean_column_name] = True
            stats_sub.loc[:, reason_column_name] = self.tables_zorder_not_effective
            return stats_sub

        self._apply_changes_to_stats(
            condition=self._stats.max_optimize_timestamp.notnull() & self._stats.p50_file_size.notnull(),
            boolean_column_name="rec_misc",
            reason_column_name="rec_misc_reason",
            f_apply_legend=check_zorder_not_effective_apply_legend,
        )

    def stats(self) -> DataFrame:
        """Ouputs the stats per table"""
        return self._spark.createDataFrame(self._stats)

    def display(self) -> None:
        """Executes the Delta housekeeping analysis and displays a sample of results"""
        return self.apply().display()

    def apply(self) -> DataFrame:
        """Displays recommendations in a DataFrame format"""
        return self._spark.createDataFrame(self.generate_recommendations())

    def generate_recommendations(self) -> pd.DataFrame:
        """
        Generates Delta Housekeeping recommendations as a list of dictionaries (internal use + unit tests only)
        A dict per recommendation where:
        - The key is the legend of the recommendation
        - The value is a pandas df with the affected tables
        """
        self._need_optimize()
        self._never_vacuumed(),
        self._not_optimized_last_days(),
        self._not_vacuumed_last_days(),
        self._optimized_too_frequently(),
        self._vacuumed_too_frequently(),
        self._optimize_not_needed(),
        self._analyze_these_tables(),
        self._zorder_not_effective(),
        return self._stats.copy()

    def _explain(self) -> Iterable[dict]:
        stats = self.generate_recommendations()
        stats_optimize = stats.loc[stats["rec_optimize"], :]
        stats_vacuum = stats.loc[stats["rec_vacuum"], :]
        stats_misc = stats.loc[stats["rec_misc"], :]
        schema = self._spark.createDataFrame(stats).schema
        out = []
        for legend_optimize in [
            self.tables_not_optimized_legend,
            self.tables_not_optimized_last_days,
            self.tables_optimized_too_freq,
        ]:
            out.append({legend_optimize: self._spark.createDataFrame(
                stats_optimize.loc[stats_optimize["rec_optimize_reason"].str.contains(legend_optimize)],
                schema
            )})

        for legend_vacuum in [
            self.tables_not_vacuumed_legend,
            self.tables_not_vacuumed_last_days,
            self.tables_vacuumed_too_freq,
        ]:
            out.append({legend_vacuum: self._spark.createDataFrame(
                stats_vacuum.loc[stats_vacuum["rec_vacuum_reason"].str.contains(legend_vacuum)],
                schema
            )})

        for legend_misc in [
            self.tables_to_analyze,
            self.tables_zorder_not_effective,
        ]:
            out.append({legend_misc: self._spark.createDataFrame(
                stats_misc.loc[stats_misc["rec_misc_reason"].str.contains(legend_misc)],
                schema
            )})

        return out

    def explain(self) -> None:
        from databricks.sdk.runtime import display

        for item in self._explain():
            for legend, df in item.items():
                display(legend)
                display(df)
