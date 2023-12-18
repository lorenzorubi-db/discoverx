from typing import Iterable
from discoverx.table_info import TableInfo

from pyspark.sql import DataFrame
from pyspark.sql.window import Window
import pyspark.sql.types as T
import pyspark.sql.functions as F




class DeltaHousekeeping:
    empty_schema = T.StructType([
        T.StructField("catalog", T.StringType()),
        T.StructField("database", T.StringType()),
        T.StructField("tableName", T.StringType()),
    ])

    @staticmethod
    def _process_describe_history(describe_detail_df, describe_history_df) -> DataFrame:
        """
        processes the DESCRIBE HISTORY result of potentially several tables in different schemas/catalogs
        Provides
        - table stats (size and number of files)
        - timestamp for last & second last OPTIMIZE
        - stats of OPTIMIZE (including ZORDER)
        - timestamp for last & second last VACUUM

        TODO reconsider if it is better outside of the class
        """
        if not "operation" in describe_history_df.columns:
            return describe_detail_df

        # window over operation
        operation_order = (
            describe_history_df
            .filter(F.col("operation").isin(["OPTIMIZE", "VACUUM END"]))
            .withColumn("operation_order", F.row_number().over(
                Window.partitionBy(["catalog", "database", "tableName", "operation"]).orderBy(F.col("timestamp").desc())
            ))
        )
        # max & 2nd timestamp of OPTIMIZE into output
        out = describe_detail_df.join(
            operation_order
            .filter((F.col("operation") == "OPTIMIZE") & (F.col("operation_order") == 1))
            .select("catalog", "database", "tableName", "timestamp")
            .withColumnRenamed("timestamp", "max_optimize_timestamp"),
            how="outer", on=["catalog", "database", "tableName"]
        )
        out = out.join(
            operation_order
            .filter((F.col("operation") == "OPTIMIZE") & (F.col("operation_order") == 2))
            .select("catalog", "database", "tableName", "timestamp")
            .withColumnRenamed("timestamp", "2nd_optimize_timestamp"),
            how="outer", on=["catalog", "database", "tableName"]
        )
        # max timestamp of VACUUM into output
        out = out.join(
            operation_order
            .filter((F.col("operation") == "VACUUM END") & (F.col("operation_order") == 1))
            .select("catalog", "database", "tableName", "timestamp")
            .withColumnRenamed("timestamp", "max_vacuum_timestamp"),
            how="outer", on=["catalog", "database", "tableName"]
        )
        out = out.join(
            operation_order
            .filter((F.col("operation") == "VACUUM END") & (F.col("operation_order") == 2))
            .select("catalog", "database", "tableName", "timestamp")
            .withColumnRenamed("timestamp", "2nd_vacuum_timestamp"),
            how="outer", on=["catalog", "database", "tableName"]
        )
        # summary of table metrics
        table_metrics_1 = (
            operation_order.filter((F.col("operation") == "OPTIMIZE") & (F.col("operation_order") == 1))
            .select([
                F.col("catalog"),
                F.col("database"),
                F.col("tableName"),
                F.col("min_file_size"),
                F.col("p50_file_size"),
                F.col("max_file_size"),
                F.col("z_order_by"),
            ])
        )

        # write to output
        out = out.join(
            table_metrics_1,
            how="outer", on=["catalog", "database", "tableName"]
        )

        return out

    def scan(
            self,
            table_info_list: Iterable[TableInfo],
            housekeeping_table_name: str = "lorenzorubi.default.housekeeping_summary_v2",  # TODO remove
            do_save_as_table: bool = True,
    ):
        dd_list = []
        statements = []
        errors = []

        if not isinstance(table_info_list, Iterable):
            table_info_list = [table_info_list]

        for table_info in table_info_list:
            try:
                dd = spark.sql(f"""
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
                dd_list.append(dd)
                statements.append(f"""
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
                """)
            except Exception as e:
                errors.append(spark.createDataFrame(
                    [(table_info.catalog, table_info.schema, table_info.table, str(e))],
                    ["catalog", "database", "tableName", "error"]
                ))

        statement = " UNION ".join(statements)

        dh = spark.createDataFrame([], self.empty_schema)
        if statements:
            dh = self.process_describe_history(
                reduce(
                    lambda left, right: left.union(right),
                    dd_list
                ),
                spark.sql(statement),
                None
            )

        errors_df = spark.createDataFrame([], self.empty_schema)
        if errors:
            errors_df = reduce(
                lambda left, right: left.union(right),
                errors
            )

        out = dh.unionByName(errors_df, allowMissingColumns=True)
        if do_save_as_table:
            (
                out
                .write
                .format("delta")
                .mode("append")
                .option("mergeSchema", "true")
                .saveAsTable(housekeeping_table_name)
            )
        return out

