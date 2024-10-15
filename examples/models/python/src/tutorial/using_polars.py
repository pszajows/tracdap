#  Copyright 2024 Accenture Global Solutions Limited
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import typing as tp
import tracdap.rt.api as trac

import tutorial.schemas as schemas

import polars


def calculate_profit_by_region_polars(
        customer_loans: "polars.DataFrame",
        eur_usd_rate: float,
        default_weighting: float,
        filter_defaults: bool):

    if filter_defaults:
        customer_loans = customer_loans.filter(polars.col("loan_condition_cat") == 0)

    # Build a weighting vector, use default_weighting for bad loans and 1.0 for good loans
    condition_weighting = customer_loans \
            .get_column("loan_condition_cat") \
            .map_elements(lambda c: default_weighting if c > 0 else 1.0, polars.Decimal)

    # Use lazy processing
    customer_loans = customer_loans.lazy() \
            .with_columns(gross_profit_unweighted = (polars.col("total_pymnt") - polars.col("loan_amount"))) \
            .with_columns(gross_profit_weighted = (polars.col("gross_profit_unweighted") * condition_weighting)) \
            .with_columns(gross_profit = (polars.col("gross_profit_weighted") * eur_usd_rate))

    profit_by_region = customer_loans \
        .group_by("region") \
        .agg(polars.col("gross_profit").sum())

    # Evaluate lazy result before giving the result to TRAC
    return profit_by_region.collect()


class UsingPolarsModel(trac.TracModel):

    def define_attributes(self) -> tp.List[trac.TagUpdate]:

        return trac.define_attributes(
            trac.A("model_description", "A example model, for testing purposes"),
            trac.A("business_segment", "retail_products", categorical=True),
            trac.A("classifiers", ["loans", "uk", "examples"], attr_type=trac.STRING)
        )

    def define_parameters(self) -> tp.Dict[str, trac.ModelParameter]:

        return trac.define_parameters(

            trac.P("eur_usd_rate", trac.FLOAT,
                   label="EUR/USD spot rate for reporting"),

            trac.P("default_weighting", trac.FLOAT,
                   label="Weighting factor applied to the profit/loss of a defaulted loan"),

            trac.P("filter_defaults", trac.BOOLEAN,
                   label="Exclude defaulted loans from the calculation",
                   default_value=False))

    def define_inputs(self) -> tp.Dict[str, trac.ModelInputSchema]:

        customer_loans = trac.load_schema(schemas, "customer_loans.csv")

        return {"customer_loans": trac.ModelInputSchema(customer_loans)}

    def define_outputs(self) -> tp.Dict[str, trac.ModelOutputSchema]:

        profit_by_region = trac.load_schema(schemas, "profit_by_region.csv")

        return {"profit_by_region": trac.ModelOutputSchema(profit_by_region)}

    def run_model(self, ctx: trac.TracContext):

        eur_usd_rate = ctx.get_parameter("eur_usd_rate")
        default_weighting = ctx.get_parameter("default_weighting")
        filter_defaults = ctx.get_parameter("filter_defaults")

        customer_loans = ctx.get_polars_table("customer_loans")

        profit_by_region = calculate_profit_by_region_polars(
            customer_loans, eur_usd_rate,
            default_weighting, filter_defaults)

        ctx.put_polars_table("profit_by_region", profit_by_region)


if __name__ == "__main__":
    import tracdap.rt.launch as launch
    launch.launch_model(UsingPolarsModel, "config/using_data.yaml", "config/sys_config.yaml")
